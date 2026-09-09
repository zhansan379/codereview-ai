"""一次性回填：为历史 `review_task` 补齐空覆盖集 `diff_snapshot`。

背景：`forges/github._new_file_content_from_patch` 修复前只对「新增文件」还原正文，
修改型文件 `new_file_content` 恒空 → `covered_file_map` 收不进 → `diff_snapshot`
一直是 `{}`。于是聚合/对比视图里，凡是「上次报过、本轮改修文件」的 finding 都因
文件不在覆盖集被判成 `not_reviewed（未验证）`，修好了也显示「存在上轮未覆盖」。
（见 `review/compare.py`、`api/admin/reviews.py` 的 not_reviewed 语义。）

本脚本按「修复后」的重建逻辑，对每个 **completed 且 diff_snapshot 为空**的 mr 任务，
用**本地 git**（默认当前仓库，`--repo` 可指）还原该 head 相对 base 的变更文件正文并重算
覆盖集，写回 `review_task.diff_snapshot`。只补空，非空一律不动（幂等）。

对齐 worker 的两个输入口径，尽量贴近「当时实际喂给 agent 的文件集」：
- 扩展名过滤：读该项目在 `project.file_extensions` 的配置，非空则 `apply_extension_filter`；
- 未变更复用：同一 PR 内按任务 id 升序处理，用上一轮回填好的覆盖集做 `prune_unchanged`
  （增量剪枝是「往少审」方向 → 更保守，不会放大「假已解决」风险）。

仅本地 git 能解析 base/head 的任务才回填；解析不了 / 重算结果为空的任务跳过并记录。

用法（默认干跑打印预览，`--apply` 才真正落库）：
    .venv/Scripts/python.exe scripts/backfill_diff_snapshot.py
    .venv/Scripts/python.exe scripts/backfill_diff_snapshot.py --apply
    .venv/Scripts/python.exe scripts/backfill_diff_snapshot.py --pr 24 --apply   # 只回填 PR#24
    .venv/Scripts/python.exe scripts/backfill_diff_snapshot.py --repo C:/path/to/repo --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections import defaultdict

from sqlalchemy import select

from codereview_ai.domain.models import ChangeType, FileDiff
from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import Project, ReviewTask
from codereview_ai.worker import apply_extension_filter
from codereview_ai.review.reuse import covered_file_map, prune_unchanged
from codereview_ai.forges.github import _new_file_content_from_patch

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")

#: name-status 首字符 → ChangeType（--no-renames 下 rename 会拆成 A + D）。
_STATUS_CHANGE = {"A": ChangeType.NEW_FILE, "M": ChangeType.MODIFIED}


def _git(repo: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """在指定 repo 跑 git；text=True 必须显式 utf-8（Windows GBK 会炸 reader 线程）。"""
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _has_commit(repo: str, sha: str) -> bool:
    proc = _git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return proc.returncode == 0


def _changed_files(repo: str, base: str, head: str) -> list[tuple[str, ChangeType]]:
    """`git diff base..head` 的变更文件（--no-renames → rename 拆 A+D）。"""
    proc = _git(repo, ["diff", "--no-renames", "--name-status", base, head])
    if proc.returncode != 0:
        return []
    out: list[tuple[str, ChangeType]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        status, path = parts[0][0], parts[1]
        ct = _STATUS_CHANGE.get(status)
        if ct is not None:
            out.append((path, ct))
    return out


async def main(url: str, repo: str, pr_only: int | None, apply: bool) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    async with session() as s:
        # 项目级扩展名过滤配置（provider, repo_id) → 扩展名字符串。
        proj_ext: dict[tuple[str, str], str] = {
            (p.provider, p.repo_id): (p.file_extensions or "")
            for p in (await s.execute(select(Project))).scalars()
        }
        rows = (await s.execute(
            select(ReviewTask)
            .where(ReviewTask.event_type == "mr", ReviewTask.state == "completed")
            .order_by(ReviewTask.provider, ReviewTask.repo_id,
                      ReviewTask.pr_number, ReviewTask.id)
        )).scalars().all()
        if pr_only is not None:
            rows = [r for r in rows if r.pr_number == pr_only]

        # 只碰空覆盖集（幂等：NULL / '' / '{}'）
        targets = [r for r in rows
                   if not r.diff_snapshot or r.diff_snapshot.strip() in ("", "{}")]
        # 同 PR 上一已完成轮的覆盖集（顺序维护，供增量剪枝）
        prev_cov: dict[tuple[str, str, int], dict[str, str]] = defaultdict(dict)

        filled = skipped = empty = 0
        reasons: list[str] = []
        for r in targets:
            base, head = r.base_sha or "", r.head_sha or ""
            key = (r.provider, r.repo_id, r.pr_number)
            if not (base and head) or not _has_commit(repo, base) or not _has_commit(repo, head):
                skipped += 1
                reasons.append(f"task#{r.id} head {head[:8]} 本地 git 无法解析，跳过")
                continue

            diffs: list[FileDiff] = []
            for path, ct in _changed_files(repo, base, head):
                if ct is ChangeType.DELETED_FILE:
                    continue  # 删除文件无新侧内容，不进覆盖集
                patch = _git(repo, ["diff", "--no-renames", base, head, "--", path]).stdout
                diffs.append(FileDiff(
                    old_path=path, new_path=path, diff=patch,
                    additions=0, deletions=0, change_type=ct,
                    new_file_content=_new_file_content_from_patch(patch, ct),
                ))
            # 对齐 worker：扩展名过滤 + 未变更复用剪枝。
            exts = proj_ext.get((r.provider, r.repo_id), "")
            if exts:
                diffs = apply_extension_filter(diffs, exts)
            last = prev_cov.get(key)
            if last:
                diffs = prune_unchanged(diffs, last)
            cov = covered_file_map(diffs)

            if not cov:
                empty += 1
                reasons.append(f"task#{r.id} head {head[:8]} 重算后覆盖集仍为空，跳过")
                continue

            if not apply:
                preview = ", ".join(sorted(cov.keys())[:6])
                extra = "…" if len(cov) > 6 else ""
                print(f"  将回填 task#{r.id} pr#{r.pr_number} {head[:8]} "
                      f"-> {len(cov)} 文件: {preview}{extra}")
            else:
                r.diff_snapshot = json.dumps(cov, ensure_ascii=False)
            prev_cov[key] = cov
            filled += 1

        if apply:
            await s.commit()

    print(f"\n共 {len(rows)} 条 completed mr 任务，其中 {len(targets)} 条覆盖集为空。")
    print(f"回填完成（{'落库' if apply else '仅预览，未改动，--apply 才写' }）：{filled} 条；"
          f"跳过 {skipped} 条（git 不可解析）；空结果 {empty} 条。")
    for line in reasons[:15]:
        print("  " + line)
    if len(reasons) > 15:
        print(f"  … 及另外 {len(reasons) - 15} 条")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="回填空覆盖集的 review_task（本地 git 重算 diff_snapshot）")
    parser.add_argument("--repo", default=os.getcwd(), help="本地 git 仓库路径（默认当前目录）")
    parser.add_argument("--db", default=DEFAULT_URL, help="DATABASE_URL")
    parser.add_argument("--pr", type=int, help="只处理指定 pr_number")
    parser.add_argument("--apply", action="store_true", help="真正落库（默认仅预览）")
    args = parser.parse_args()
    asyncio.run(main(args.db, args.repo, args.pr, args.apply))
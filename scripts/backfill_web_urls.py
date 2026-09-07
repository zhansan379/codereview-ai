"""一次性回填：为存量审查记录补上 `web_url`（详情页「直达链接」显示用）。

背景：`review_task` 新加 `web_url` 列后，仅**新增**记录会在入库时写入链接（mr 轨=
forge 给出的 MR/PR 页；push 轨=`{项目 web_url}/commit/{head_sha}`，见 worker.py）。
存量行该列为默认空串，详情页链接不显示。本脚本把存量行按同一规则补齐：

- **mr 轨**：优先从原始 webhook `payload` 里取**当时捕获的确切** URL
  （GitLab `object_attributes.url`、GitHub `pull_request.html_url`），与入库值一致、最准；
  payload 缺失/形状不符时回退按项目仓库主页 + provider 路径模板重建
  （github/gitee → `{主页}/pull/{pr_number}`；gitlab → `{主页}/-/merge_requests/{pr_number}`）。
- **push 轨**：无 PR，按 `{项目 web_url}/commit/{head_sha}` 重建。

依赖项目表 `(provider, repo_id) → web_url`；项目缺行或 `web_url` 为空时该行跳过、保持空。

用法：
    .venv/Scripts/python.exe scripts/backfill_web_urls.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）

幂等：只处理 `web_url=''` 的行；处理后非空，二跑不再入选。已处理行若 `payload` 中仍含
确切 URL 而 project 主页空，同样能补齐（不依赖项目表）。
"""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import select

from codereview_ai.storage.db import _ensure_latest_schema, create_engine, session_factory
from codereview_ai.storage.models import Project, ReviewTask

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")


def _payload_mr_url(row: ReviewTask) -> str:
    """从 mr 轨原始 webhook payload 提取当时的 MR/PR 页 URL（与入库值同源）。"""
    if not row.payload:
        return ""
    try:
        data = json.loads(row.payload)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    oa = data.get("object_attributes")  # GitLab
    if isinstance(oa, dict) and oa.get("url"):
        return str(oa["url"])
    pr = data.get("pull_request")  # GitHub
    if isinstance(pr, dict) and pr.get("html_url"):
        return str(pr["html_url"])
    return ""


def _reconstruct_mr(project_url: str, provider: str, pr_number: int | None) -> str:
    """payload 缺失时的 mr 回退：`{项目主页} + provider 路径模板`。"""
    if not project_url or not pr_number:
        return ""
    base = project_url.rstrip("/")
    if provider == "gitlab":
        return f"{base}/-/merge_requests/{pr_number}"
    if provider in ("github", "gitee"):
        return f"{base}/pull/{pr_number}"
    return ""


def _reconstruct_push(project_url: str, head_sha: str) -> str:
    """push 轨：`{项目主页}/commit/{head_sha}`（与入库值同规则）。"""
    if not project_url or not head_sha:
        return ""
    return f"{project_url.rstrip('/')}/commit/{head_sha}"


async def main(url: str) -> None:
    engine = create_engine(url)
    await _ensure_latest_schema(engine)  # 存量库补出新增列 web_url（幂等）
    session = session_factory(engine)

    async with session() as s:
        # 项目主页映射：(provider, repo_id) → web_url
        project_url: dict[tuple[str, str], str] = {}
        for p in (await s.execute(select(Project))).scalars():
            if p.web_url:
                project_url[(p.provider, p.repo_id)] = p.web_url

        rows = (await s.execute(
            select(ReviewTask).where(ReviewTask.web_url == "")
        )).scalars().all()

        counts = {"mr_payload": 0, "mr_reconstruct": 0, "push": 0, "skipped": 0}
        samples: list[str] = []
        for row in rows:
            url = ""
            src = ""
            base = project_url.get((row.provider, row.repo_id), "")
            if row.event_type == "mr":
                url = _payload_mr_url(row)
                if url:
                    src = "mr_payload"
                else:
                    url = _reconstruct_mr(base, row.provider, row.pr_number)
                    src = "mr_reconstruct" if url else ""
            elif row.event_type == "push":
                url = _reconstruct_push(base, row.head_sha)
                src = "push" if url else ""
            if url:
                row.web_url = url
                counts[src] += 1
                if len(samples) < 3:
                    samples.append(f"#{row.id} [{row.provider}/{row.event_type}] → {url}")
            else:
                counts["skipped"] += 1
        await s.commit()

    print("回填审查记录 web_url 完成：")
    print(f"  mr（payload 确切 URL）: {counts['mr_payload']}")
    print(f"  mr（项目主页重建）    : {counts['mr_reconstruct']}")
    print(f"  push（提交链接）      : {counts['push']}")
    print(f"  未处理（无来源）      : {counts['skipped']}")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))
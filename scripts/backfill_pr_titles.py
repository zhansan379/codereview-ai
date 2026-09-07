"""一次性回填：从存量 review_task.payload（原始 webhook JSON）提取 PR 标题写入 pr_title。

背景：worker 落库 `ensure_task` 曾丢弃 `PullRequest.title`，`review_task.pr_title` 列此后
新增（DB 存量行该列恒为空）；PR 标题实际藏在 `payload` 里。修复后新审查自动写入（PR #?），
本脚本把存量 mr 轨标题从 payload 提取回来。

提取路径（与 forge 解析一致，见 github.py:44/64、gitlab.py:37/53）：
- github：`payload.pull_request.title`
- gitlab：`payload.object_attributes.title`
两处皆无 → 留空（WARNING 记一行，不强行编造）。

用法：
    .venv/Scripts/python.exe scripts/backfill_pr_titles.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）

幂等：只处理 `event_type='mr'` 且 `pr_title=''` 的行；写出后不再入选。
"""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import select

from codereview_ai.storage.db import _ensure_latest_schema, create_engine, session_factory
from codereview_ai.storage.models import ReviewTask

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")


def _extract_title(payload_raw: str) -> str:
    """从原始 webhook JSON 提取 PR 标题；都要无则返回 ''。"""
    if not payload_raw:
        return ""
    try:
        d = json.loads(payload_raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(d, dict):
        return ""
    pr = d.get("pull_request")
    if isinstance(pr, dict) and isinstance(pr.get("title"), str):
        t = pr["title"].strip()
        if t:
            return t
    oa = d.get("object_attributes")
    if isinstance(oa, dict) and isinstance(oa.get("title"), str):
        t = oa["title"].strip()
        if t:
            return t
    if isinstance(d.get("title"), str):
        t = d["title"].strip()
        if t:
            return t
    return ""


async def main(url: str) -> None:
    engine = create_engine(url)
    await _ensure_latest_schema(engine)  # 存量库补出新增列 pr_title（幂等）
    session = session_factory(engine)
    changed: int = 0
    skipped: int = 0
    samples: list[str] = []
    async with session() as s:
        stmt = select(ReviewTask).where(
            ReviewTask.event_type == "mr", ReviewTask.pr_title == ""
        )
        rows = (await s.execute(stmt)).scalars().all()
        for row in rows:
            title = _extract_title(row.payload or "")
            if not title:
                print(f"  WARNING 任务 #{row.id} (repo={row.repo_id} pr={row.pr_number}) "
                      f"payload 无标题，保留空")
                skipped += 1
                continue
            if len(samples) < 3:
                samples.append(f"#{row.id} → {title}")
            row.pr_title = title
            changed += 1
        await s.commit()
    print(f"已回填 {changed} 条 review_task.pr_title（跳过 {skipped} 条 payload 无标题）")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))
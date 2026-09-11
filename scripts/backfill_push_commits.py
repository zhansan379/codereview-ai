"""一次性回填：把存量 push 轨的 pr_title（历史曾塞 commit 消息）迁到 push_commits。

背景：早期版本把 push 轨的提交消息（`_commits_text`，多行过长）写进 `pr_title`，导致
表格「标题」列与详情头部被整段 commit 刷屏。修复后 push 轨 `pr_title` 留空、commit 消息
落进独立列 `push_commits` 由详情页单独展示（PR #?）。本脚本把存量遗留行做同样迁移：
`pr_title != ''` → 移到 `push_commits` 并清空 `pr_title`。

用法：
    .venv/Scripts/python.exe scripts/backfill_push_commits.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）

幂等：只处理 `event_type='push'` 且 `push_commits=''` 且 `pr_title!=''` 的行；处理后
`pr_title` 清空、`push_commits` 非空，二跑不再入选。
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")


async def main(url: str) -> None:
    engine = create_engine(url)
    await init_db(engine)  # 确保表结构与 push_commits 列存在（schema 稳定后无需补列）
    session = session_factory(engine)
    changed: int = 0
    samples: list[str] = []
    async with session() as s:
        stmt = select(ReviewTask).where(
            ReviewTask.event_type == "push",
            ReviewTask.push_commits == "",
            ReviewTask.pr_title != "",
        )
        rows = (await s.execute(stmt)).scalars().all()
        for row in rows:
            if len(samples) < 3:
                samples.append(f"#{row.id} → {row.pr_title[:40]}{'…' if len(row.pr_title) > 40 else ''}")
            row.push_commits = row.pr_title
            row.pr_title = ""
            changed += 1
        await s.commit()
    print(f"已迁移 {changed} 条 push 轨 commit 消息 pr_title → push_commits（并清空 pr_title）")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))
"""一次性清理：删除数据库中所有审查记录。

删除范围（审查链路产生的全部数据）：
    - review_task        审查任务
    - review_finding     审查发现（CASCADE 关联 task）
    - review_conversation agent 审查对话（CASCADE 关联 task）
    - model_usage        LLM 用量（审查产生的调用记录，全量删除）

不删除：project / model_config / notifier_config / forge_config / project_rule /
schedule_job / app_setting / clone_cache_repo（非审查数据，保留）。

用法：
    .venv/Scripts/python.exe scripts/cleanup_review_records.py [--url sqlite:///./data/app.db]
"""

from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import delete, func, select

from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import (
    ModelUsage,
    ReviewConversation,
    ReviewFinding,
    ReviewTask,
)

DEFAULT_URL = os.environ.get("CR_DATABASE_URL", "sqlite:///./data/app.db")


async def main(url: str) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    async with session() as s:
        n_tasks = (await s.execute(select(func.count()).select_from(ReviewTask))).scalar_one()
        n_findings = (await s.execute(select(func.count()).select_from(ReviewFinding))).scalar_one()
        n_convs = (
            await s.execute(select(func.count()).select_from(ReviewConversation))
        ).scalar_one()
        n_usage = (await s.execute(select(func.count()).select_from(ModelUsage))).scalar_one()

        # 删除：子表先于 task（CASCADE 亦已覆盖，这里显式删确保 SQLite 生效）
        await s.execute(delete(ReviewFinding))
        await s.execute(delete(ReviewConversation))
        await s.execute(delete(ModelUsage))
        await s.execute(delete(ReviewTask))
        await s.commit()

        print(f"已删除审查记录：task {n_tasks}、finding {n_findings}、"
              f"conversation {n_convs}、model_usage {n_usage}")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="删除数据库中所有审查记录")
    parser.add_argument("--url", default=DEFAULT_URL, help="数据库 URL")
    args = parser.parse_args()
    asyncio.run(main(args.url))

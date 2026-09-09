"""一次性清理：把未真正执行审查的 `review_task.exec_mode` 误标值（默认 'diff'）重置为 NULL。

背景：`exec_mode` 列此前默认值为 `'diff'`，导致**未真正执行审查**的行（skipped / 早期
failed / queued / completed-empty 空审，即未走到 `set_exec_metrics` 的行）也被错误标成
'diff'，既在列表/详情误显示为 Diff 模式，也被统计进模式分布图。修复后默认值改为 NULL
（见 `storage/models.py`），只有真正执行审查才填 agentic / diff；本脚本把存量误标行清掉。

清理规则（保守、只清确定误标）：`exec_mode='diff'` 且 `state != 'completed'` → 置 NULL。
理由：`skipped`/`failed`/`queued` 语义即未走完审查路径，exec_mode 必为默认误标；
`completed` 可能是真 diff 审查或 empty 空审，无法可靠区分，为防误删**保留**。
（真执行过 diff 审查的行 state 必为 completed。）

用法：
    .venv/Scripts/python.exe scripts/backfill_clear_false_exec_mode.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）

幂等：只处理 `exec_mode='diff'` 且 `state != 'completed'` 的行；处理后不再入选。
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import ReviewTask

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")


async def main(url: str) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    changed: int = 0
    samples: list[str] = []
    async with session() as s:
        stmt = select(ReviewTask).where(
            ReviewTask.exec_mode == "diff", ReviewTask.state != "completed"
        )
        rows = (await s.execute(stmt)).scalars().all()
        for row in rows:
            if len(samples) < 3:
                samples.append(f"#{row.id} state={row.state}")
            row.exec_mode = None
            changed += 1
        await s.commit()
    print(f"已清理 {changed} 条 review_task.exec_mode → NULL（误标 diff）")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))

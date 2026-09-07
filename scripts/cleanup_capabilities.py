"""一次性清理：移除 model_config.capabilities 里历史的「总结/测试」业务能力标签。

背景：早期模型页把 capabilities 做成 review/summary/test 的业务能力多选，但代码里
**没有任何后端逻辑按它分派**（全仓真实消费模型的只有「审查」一条，reviewer.py）。
且该字段设计语义本应是模型接口能力（json_object/tool_calls/streaming，DESIGN §16）。
模型页已移除该多选，本脚本把存量行里残留的 summary/test 值清掉，只保留 review
（兼容列表与 legacy 字典两种存储形态）。幂等：处理后无 summary/test，二跑不入选。

用法：
    .venv/Scripts/python.exe scripts/cleanup_capabilities.py
    # 或指定库： DATABASE_URL="sqlite:///./data/app.db" 同上（默认已指向该库）
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import select

from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import ModelConfig

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")

#: 需清除的业务能力标签（历史多选遗留，无代码分派，见模块 docstring）
_REDUNDANT = {"summary", "test"}


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        kept = {k: v for k, v in value.items() if k not in _REDUNDANT}
        return kept if kept else {}
    if isinstance(value, list):
        kept = [x for x in value if x not in _REDUNDANT]
        return kept
    return value  # 非 list/dict 的异常形态不动


async def main(url: str) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    changed: int = 0
    samples: list[str] = []
    async with session() as s:
        rows = (await s.execute(select(ModelConfig))).scalars().all()
        for row in rows:
            scrubbed = _scrub(row.capabilities)
            if scrubbed == row.capabilities:
                continue
            if len(samples) < 5:
                samples.append(f"#{row.id} {row.name}: {row.capabilities!r} → {scrubbed!r}")
            row.capabilities = scrubbed
            changed += 1
        await s.commit()
    print(f"已清理 {changed} 行 model_config.capabilities（移除 summary/test）")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL))
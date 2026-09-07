"""一次性回填：把存量 review_finding.title（已是全文分析）压缩成 LLM 简短标题。

背景：早期 `title == detail == content`（全文），详情页「分析」列塞满长篇。修复见
PR #title：LLM 审查起新产出携带 `Finding.title`（≤30 字短标题），落库 `title=短标题、
detail=全文`。存量旧行 title 仍是全文，用本脚本逐条调 LLM 压缩成短标题写回，使其与
新审查一致。

用法：
    .venv/Scripts/python.exe scripts/backfill_finding_titles.py
    # 指定模型： CR_LLM_MODEL="gpt-4o-mini" 同上；api_key 走既有 env/litellm 配置

幂等：只处理 `title == detail`（全文）或 `length(title) > 40` 的行；回填后 title != detail
且更短，二次运行 0 变更。依赖真实 LLM，需网络与鉴权；单条失败仅告警不中断。
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import func, select

from codereview_ai.review.llm_gateway import LLMError, LLMGateway
from codereview_ai.storage.db import create_engine, session_factory
from codereview_ai.storage.models import ReviewFinding

DEFAULT_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
DEFAULT_MODEL = os.environ.get("CR_LLM_MODEL") or "gpt-4o-mini"

#: 目标候选——回填前的全文行（title 仍是 detail 全文，或远超短标题长度）。
_SELECT_STMT = select(ReviewFinding).where(
    (ReviewFinding.title == ReviewFinding.detail) | (func.length(ReviewFinding.title) > 40)
)

_PROMPT = (
    "你是代码审查问题摘要器。把下面这条 code review 发现压缩成一个 ≤30 字的简短标题，"
    "作为表格列展示用。要求：中文、概述核心问题、不含代码、不含换行、不出现 JSON/md 围栏。"
    "只输出标题本身，不要任何其他文字。\n\n{}"
)


class _Backend:
    """测试用假 backend：构造时「模型调用」返回固定标题，便于离线断言。

    `responses` 为迭代器，每次调用取下一个；耗尽时复用最后一值。
    """

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = responses or ["测试标题"]

    async def __call__(self, messages: list[dict]) -> str:
        out = self.responses[0] if len(self.responses) == 1 else None
        return out if out is not None else self.responses.pop(0)


def _shorten_fulltext(text: str, keep: int = 39) -> str:
    """兜底：LLM 调用失败时把全文截短，保证不把长文留在 title。"""
    text = text.strip()
    return text if len(text) <= keep else text[: keep - 1] + "…"


async def _make_gateway(model: str, backend: _Backend | None) -> LLMGateway:
    return LLMGateway(model=model, backend=backend, max_tokens=80, temperature=0.2)


async def main(url: str, model: str, backend: _Backend | None) -> None:
    engine = create_engine(url)
    session = session_factory(engine)
    gateway = await _make_gateway(model, backend)
    changed: int = 0
    failed: int = 0
    samples: list[str] = []
    async with session() as s:
        rows = (await s.execute(_SELECT_STMT)).scalars().all()
        for row in rows:
            try:
                out = (await gateway.complete([
                    {"role": "system", "content": _PROMPT.format(row.detail or row.title)},
                ])).strip()
            except LLMError as exc:
                print(f"  WARNING 行 {row.id} LLM 失败: {exc}\n  → 用截断兜底")
                out = _shorten_fulltext(row.detail or row.title)
                failed += 1
            out = out.split("\n")[0][:40] or _shorten_fulltext(row.detail or row.title)
            if not out or out == row.title:
                continue
            if len(samples) < 3:
                samples.append(f"#{row.id} {row.title[:20]}… → {out}")
            row.title = out
            changed += 1
        await s.commit()
    print(f"已回填 {changed} 条 review_finding.title（失败 {failed} 条，用截断兜底）")
    for line in samples:
        print("  " + line)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(DEFAULT_URL, DEFAULT_MODEL, None))
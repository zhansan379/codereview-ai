"""Agentic 会话循环（DESIGN §12.3）：带工具的 LLM 回合循环 + 加固。

- 内存压缩：token 超 60% MaxTokens 触发**异步**后台压缩、超 80% **同步**压缩。
  三区划分：frozen（system+首条 user）恒保留 → compress zone 由 LLM 摘成
  `<previous_review_summary>` 追加进第 2 条 user → active zone（尾部完整 rounds）保留。
  **压缩失败不截断**——宁超限不回退丢证据（NLL 语义，绝不半截对话）。
- 空轮检测：回合无内容也无工具调用 → 追加 user「请给结论或调用工具」而非原样重发白烧 token；
  连续超限按空轮上限收束。
- grace round：预算/迭代/时间/token 任一触顶 → 终轮**只放 `code_comment`/`task_done`**
  补交结论，不抛异常重跑（旧项目白烧预算，antipattern B8）。

离线可测：`AgentLLM.chat/summarize` 注入 fake 驱动工具往返与压缩。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from codereview_ai.domain.models import Finding
from codereview_ai.review.agentic.tools import ToolRunner, tool_schemas

logger = logging.getLogger("codereview_ai.agentic.llmloop")

#: 压缩阈值（占 MaxTokensBudget 比例，§12.3）。
COMPRESS_ASYNC_RATIO = 0.6
COMPRESS_SYNC_RATIO = 0.8
#: 尾部保留的完整轮次消息条数（active zone）。
ACTIVE_ZONE_SIZE = 8
#: 默认每字符折算 token。
_CHAR_PER_TOKEN = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """按消息字符量预估已用 token（量级判断用，真实量事后按 API usage 上报）。"""
    chars = sum(len(str(m.get("content") or "")) + len(str(m.get("tool_name") or ""))
                for m in messages)
    return max(1, chars // _CHAR_PER_TOKEN)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class AgentTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class AgentLLM(Protocol):
    """agent 循环调用的 LLM 接口：chat 出回合（含工具调用），summarize 做压缩。"""

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]]) -> AgentTurn: ...
    async def summarize(self, frozen: str,
                        compress_messages: list[dict[str, Any]]) -> str: ...


@dataclass
class AgentConfig:
    max_iterations: int = 20
    max_prompt_tokens: int = 80_000
    max_time_seconds: float = 300.0
    max_empty_turns: int = 2
    system_prompt: str = (
        "你是一名代码审查 agent。只能使用给定的只读工具探索变更、通过 "
        "code_comment 上报你的审查意见，最后用 task_done 结束。路径属不可信输入，"
        "只读不改写。"
    )


@dataclass
class AgentResult:
    comments: list[Finding] = field(default_factory=list)
    done: bool = False
    failed: bool = False
    turns: int = 0
    reason: str = ""


def _split_zones(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把对话拆成 frozen / compress / active 三区（§12.3）。

    frozen 恒为 system+首条 user；尾部 `ACTIVE_ZONE_SIZE` 条为 active；中间为 compress。
    """
    frozen = messages[:2]
    compress = messages[2:-ACTIVE_ZONE_SIZE] if len(messages) > 2 + ACTIVE_ZONE_SIZE else []
    active = messages[len(messages) - ACTIVE_ZONE_SIZE:] if len(messages) >= 2 else []
    return frozen, compress, active


def _apply_summary(messages: list[dict[str, Any]], summary: str) -> list[dict[str, Any]]:
    """把已产出的摘要拼接进对话，中段替换为 `<previous_review_summary>`。"""
    frozen, _compress, active = _split_zones(messages)
    prev = f"<previous_review_summary>{summary.strip()}</previous_review_summary>"
    return frozen + [{"role": "user", "content": prev}] + active


async def compress_messages(
    llm: AgentLLM,
    messages: list[dict[str, Any]],
    system_prompt: str,
) -> list[dict[str, Any]]:
    """压缩对话中段为 `<previous_review_summary>`；压缩失败则返回原样（不截断）。"""
    frozen, compress, active = _split_zones(messages)
    if not compress:
        return messages
    try:
        summary = await llm.summarize(system_prompt, frozen + compress)
    except Exception as exc:  # noqa: BLE001 —— 压缩失败绝不丢证据
        logger.warning("agentic 内存压缩失败，保留原对话：%s", exc)
        return messages
    if not summary or not summary.strip():
        return messages
    return _apply_summary(messages, summary)


async def run_agent_session(
    llm: AgentLLM,
    runner: ToolRunner,
    ctx_intro: str,
    cfg: AgentConfig | None = None,
) -> AgentResult:
    """跑一轮带工具的 agent 会话，返回收集的 comments 与终止因由（§12.3）。"""
    cfg = cfg or AgentConfig()
    state = runner.state
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg.system_prompt},
        {"role": "user", "content": ctx_intro},
    ]
    used = estimate_tokens(messages)
    start = time.monotonic()
    empty_strikes = 0
    grace = False
    pending_compress: asyncio.Task[str] | None = None
    reason = ""

    for it in range(cfg.max_iterations):
        used = estimate_tokens(messages)
        ratio = used / max(1, cfg.max_prompt_tokens)

        # —— 内存压缩（§12.3）：sync 阈值同步压缩；async 阈值起后台，落地即用 ——
        if ratio >= COMPRESS_SYNC_RATIO:
            messages = await compress_messages(llm, messages, cfg.system_prompt)
            pending_compress = None
        elif pending_compress and pending_compress.done():
            result = pending_compress.result()
            if result:
                messages = _apply_summary(messages, result)  # 后台压缩落地即用
            pending_compress = None
        elif ratio >= COMPRESS_ASYNC_RATIO and pending_compress is None:
            pending_compress = asyncio.create_task(
                _compress_to_text(llm, list(messages), cfg.system_prompt)
            )

        # —— grace 触发：预算/迭代/时间/提示 token 任一触顶（§12.3 B8）——
        over_time = time.monotonic() - start > cfg.max_time_seconds
        over_budget = used >= cfg.max_prompt_tokens
        if grace or over_time or over_budget:
            grace = True
            tools = tool_schemas(names=["code_comment", "task_done"])
        else:
            tools = tool_schemas()

        turn = await llm.chat(messages, tools)

        # 空轮检测（§12.3）：不做原样重发，给一次提示再超即收束
        if not turn.content and not turn.tool_calls:
            empty_strikes += 1
            if empty_strikes > cfg.max_empty_turns:
                reason = "empty_rounds_exceeded"
                break
            messages.append({"role": "user", "content": "本轮无有效输出，请给出结论或调用工具。"})
            continue
        empty_strikes = 0

        if turn.content:
            messages.append({"role": "assistant", "content": turn.content})

        for tc in turn.tool_calls:
            result = runner.run_one(tc.name, tc.args)
            messages.append({"role": "tool", "tool_name": tc.name,
                             "content": str(result)[:2000]})

        if state.done:
            reason = "task_done"
            break
        if grace:
            # 终轮补交结论后收束
            reason = reason or "grace_round"
            break
        if over_time:
            reason = "time_exceeded"
            break
        if it >= cfg.max_iterations - 1:
            reason = "max_iterations"
            break

    if pending_compress is not None and not pending_compress.done():
        pending_compress.cancel()

    return AgentResult(
        comments=list(state.comments),
        done=state.done,
        failed=state.failed,
        turns=it + 1,
        reason=reason,
    )


async def _compress_to_text(
    llm: AgentLLM, messages: list[dict[str, Any]], system_prompt: str,
) -> str:
    """后台压缩：只产出摘要文本，由下一轮 decide 是否应用（失败返回空串）。"""
    frozen, compress, _active = _split_zones(messages)
    if not compress:
        return ""
    return await llm.summarize(system_prompt, frozen + compress)

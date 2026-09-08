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
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from codereview_ai.domain.models import Finding
from codereview_ai.review.agentic.prompts import MAIN_TASK_SYSTEM
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
    """一次工具调用。`id` 是 OpenAI 工具调用契约的关联 id：assistant 的 `tool_calls`
    数组里声明它，随后的 `role=tool` 结果消息靠 `tool_call_id` 指回它配对。真实 LLM
    返回时由 adapter 从响应的 `call.id` 透传；离线/回放为空时由循环按序补造。
    """

    name: str
    args: dict[str, Any]
    id: str = ""
    #: 原始 arguments JSON 串：保留 API 原文减少再序列化偏差；空则回退 json.dumps(args)。
    raw_arguments: str = ""


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
    #: 单次审查内，多个文件组**并发**跑的上限（sandbox.run_agentic_review 用有界
    #: asyncio.Semaphore 控制；4=同一时刻最多 4 组在飞，避免打爆 LLM 速率）。
    #: 与 OCR 的 MaxConcurrency（goroutine 信号量）对应；跨 MR 并发仍由 worker_pool 管。
    group_concurrency: int = 4
    #: OCR 四阶段机开关（plan / re_location / review_filter / scoring 各一次 LLM 调用，
    #: 可独立关以省 token，默认全开）。
    plan_enabled: bool = True
    relocation_enabled: bool = True
    review_filter_enabled: bool = True
    scoring_enabled: bool = True
    #: plan 门控阈值（plan_required：单大文件 / 组 churn）。
    plan_line_threshold: int = 300
    plan_group_line_threshold: int = 600
    #: 主任务 system 提示词 = OCR main_task_system.md 全文（Role/Capabilities/Strict
    #: Focus Rules/Reply limit：逐文件不跳过、不改删代码、task_done 前确认每个 <file>
    #: 都过了一遍——对症"漏文件/只审第一条"）。
    system_prompt: str = MAIN_TASK_SYSTEM


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
    active 左边界向左回吸：若起点是 `role=tool` 结果，则并入其配对 assistant(tool_calls)，
    防止压缩把 `tool_call_id` 配对砍断产生孤儿 tool 消息（真实 LLM 会拒收不成对者）。
    """
    frozen = messages[:2]
    active_start = len(messages) - ACTIVE_ZONE_SIZE
    while active_start > 2 and messages[active_start]["role"] == "tool":
        active_start -= 1
    compress = messages[2:active_start]
    active = messages[active_start:]
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
        # 迭代上限触顶也进 grace：否则爱"只探索不收尾"的模型会一路读到 max_iterations
        # 空手 break（reason=max_iterations、0 条），从不走 code_comment。末轮只放
        # code_comment/task_done，逼它补交结论。
        over_time = time.monotonic() - start > cfg.max_time_seconds
        over_budget = used >= cfg.max_prompt_tokens
        if grace or over_time or over_budget or it >= cfg.max_iterations - 1:
            if not grace:
                # 首次切 grace 给一句明确指令：只补交意见+结束，别再探索
                messages.append({
                    "role": "user",
                    "content": "已达审查轮次/预算上限。请立即用 code_comment 补交你的"
                               "审查意见（已读到的内容足够下结论），然后用 task_done 结束。",
                })
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

        if turn.content or turn.tool_calls:
            # OpenAI 工具调用契约：assistant 带 tool_calls 时，每条要有独立 `id`；
            # 工具结果消息用 `tool_call_id` 指回它配对（真实 LLM 靠此还原引用的参数）。
            # 带 tool_calls 且无正文时 content 置 null（OpenAI/DeepSeek 契约，空串或会触发校验）。
            assistant: dict[str, Any] = {"role": "assistant", "content": turn.content or None}
            if turn.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (tc.raw_arguments
                                         or json.dumps(tc.args, ensure_ascii=False)),
                        },
                    }
                    for i, tc in enumerate(turn.tool_calls)
                ]
            messages.append(assistant)

        for i, tc in enumerate(turn.tool_calls):
            result = runner.run_one(tc.name, tc.args)
            # 工具结果原样进上下文（同上游 OCR：限量只发生在工具层——read_file 每文件
            # ≤500 行、grep ≤100 条，返回自带 truncated 标记；这里不再叠字节截断，
            # 避免把合法 JSON 切成畸形、也让模型看到完整自明的结果）。
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id or f"call_{i}",  # 与上方 assistant.tool_calls 同 id
                "name": tc.name,
                "content": str(result),
            })

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

    result = AgentResult(
        comments=list(state.comments),
        done=state.done,
        failed=state.failed,
        turns=it + 1,
        reason=reason,
    )
    # 会话收尾打一行可诊断日志：终止原因 + 评论数，让 0 条随手可见（模型未调 code_comment
    # 时 reason 常见 max_iterations/grace_round，据此定位「模型不产出工具调用」）。
    logger.info(
        "agent 会话结束: turns=%d reason=%s comments=%d done=%s",
        result.turns, result.reason, len(result.comments), result.done,
    )
    return result


async def _compress_to_text(
    llm: AgentLLM, messages: list[dict[str, Any]], system_prompt: str,
) -> str:
    """后台压缩：只产出摘要文本，由下一轮 decide 是否应用（失败返回空串）。"""
    frozen, compress, _active = _split_zones(messages)
    if not compress:
        return ""
    return await llm.summarize(system_prompt, frozen + compress)

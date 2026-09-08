"""AgentLLM 适配器：把现有 LLM 传输层包装成支持工具调用的会话接口（DESIGN §12.3）。

- `ToolCallingLLM` 实现 `llmloop.AgentLLM` protocol：
  - `chat(messages, tools)` —— 走 `litellm.acompletion(..., tools=...)`，把响应的
    `tool_calls` 解析成 `AgentTurn`/`ToolCall`，失败归一为 `LLMError`（由主链降级）。
  - `summarize(system_prompt, compress_messages)` —— 无工具轻量调用，按 OCR memory_compression
    五维契约产出结构化压缩摘要（已确认问题带 file+severity）。
- 复用 `llm_gateway.LLMError` 的失败语义，与 diff 审查路径一致。
- 构造参数对齐 `ResolvedLLM`（model/api_key/base_url/max_tokens/temperature），
  `build_agent_llm()` 供生产接线从 DB 解析结果直接建实例。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from codereview_ai.config.repository import ResolvedLLM
from codereview_ai.review.agentic.capture import ACTIVE_PHASE, ACTIVE_RECORDER
from codereview_ai.review.agentic.llmloop import AgentTurn, ToolCall
from codereview_ai.review.agentic.prompts import MEMORY_COMPRESSION_SYSTEM
from codereview_ai.review.llm_gateway import LLMError

logger = logging.getLogger("codereview_ai.agentic.adapter")

#: 工具调用的 backend 契约：async (messages, tools) -> 原始响应对象（离线可注入 fake）。
ToolChatBackend = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[Any]]


def _usage(resp: Any) -> dict[str, int] | None:
    """宽松取 LiteLLM 响应的 usage 计费（可能缺字段，失败返回 None）。"""
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return None
        return {
            "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
        }
    except (TypeError, ValueError):
        return None


def _litellm_tool_backend(
    *,
    model: str,
    api_key: str = "",
    base_url: str = "",
    max_tokens: int | None = None,
    temperature: float | None = None,
    json_object: bool = False,
) -> ToolChatBackend:
    """默认工具调用 backend：走 LiteLLM `acompletion(tools=...)`，返回原始响应。"""

    async def backend(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        try:
            import litellm
        except ImportError as exc:  # 未安装 litellm 却无注入 → 明确报错
            raise LLMError("litellm not installed but no backend injected") from exc
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if json_object:
            kwargs["response_format"] = {"type": "json_object"}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        return await litellm.acompletion(**kwargs)

    return backend


class ToolCallingLLM:
    """带工具调用的会话 LLM（满足 `llmloop.AgentLLM`）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        backend: ToolChatBackend | None = None,
    ) -> None:
        self.model = model
        self._backend = backend or _litellm_tool_backend(
            model=model, api_key=api_key, base_url=base_url,
            max_tokens=max_tokens, temperature=temperature,
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AgentTurn:
        """一轮带工具的 LLM 调用；失败统一抛 `LLMError`（交由会话/主链降级）。"""
        try:
            resp = await self._backend(messages, tools)
        except LLMError:
            raise
        except Exception as exc:  # 网络/超时/鉴权一律归一
            raise LLMError(f"agent chat failed: {exc}") from exc
        message = resp.choices[0].message
        content = getattr(message, "content", None) or ""
        calls = [] if not getattr(message, "tool_calls", None) else message.tool_calls
        tool_calls: list[ToolCall] = []
        for call in calls:
            name = str(getattr(call, "function", None) and call.function.name)
            raw_args = str(getattr(call.function, "arguments", None) or "")
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                args = {}  # 坏参数不中断整轮，交给工具侧缺省/报错
            if not isinstance(args, dict):
                args = {}
            # 透传 API 给的 call.id（OpenAI 契约：tool 消息凭它配对），离线为空由循环补造
            tool_calls.append(ToolCall(
                name=name, args=args,
                id=str(getattr(call, "id", "") or ""),
                raw_arguments=raw_args,
            ))
        logger.info(
            "agent chat[%s]: reply content_len=%d tool_calls=%d %s",
            self.model, len(str(content)), len(tool_calls),
            [t.name for t in tool_calls],
        )
        await self._capture(
            messages, tools,
            {"content": content,
             "tool_calls": [{"name": t.name, "args": t.args, "id": t.id,
                             "raw_arguments": t.raw_arguments} for t in tool_calls],
             "usage": _usage(resp)},
        )
        return AgentTurn(content=str(content), tool_calls=tool_calls)

    async def summarize(self, system_prompt: str, compress_messages: list[dict[str, Any]]) -> str:
        """对压缩区做一次无工具摘要调用；失败抛 `LLMError`（调用方「压缩失败不截断」）。

        用 OCR memory_compression 五维契约（已确认问题带 file+severity / 工具结论 /
        已完成 / 待办 / 当前焦点）摘摘要，保证压缩不丢已确认的证据；`compress_messages`
        即模板 user 侧的 `{{context}}`（`_split_zones` 的 frozen 已含原 system+首条 user）。
        """
        messages = [
            {"role": "system", "content": system_prompt or "你是代码审查 agent。"},
            {"role": "user", "content": MEMORY_COMPRESSION_SYSTEM},
            *compress_messages,
        ]
        try:
            resp = await self._backend(messages, [])
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"agent summarize failed: {exc}") from exc
        msg = resp.choices[0].message
        text = str(getattr(msg, "content", None) or "").strip()
        await self._capture(
            messages, [],
            {"content": text, "tool_calls": [], "usage": _usage(resp)},
            phase="compress",
        )
        return text

    async def _capture(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        response: dict[str, Any],
        *,
        phase: str | None = None,
    ) -> None:
        """把本轮 request/response 记入作用域内的对话采集器（无采集器则 no-op）。"""
        rec = ACTIVE_RECORDER.get()
        if rec is None:
            return
        await rec.record(
            phase or ACTIVE_PHASE.get() or "loop",
            request=messages,
            response=response,
            model=self.model,
        )


def build_agent_llm(
    resolved: ResolvedLLM | None, *, backend: ToolChatBackend | None = None,
) -> ToolCallingLLM | None:
    """从解析出的 LLM 配置构造成 `ToolCallingLLM`；无可用模型返回 None（上层据此降级）。"""
    if resolved is None or not resolved.model:
        return None
    return ToolCallingLLM(
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        max_tokens=resolved.max_tokens,
        temperature=resolved.temperature,
        backend=backend,
    )

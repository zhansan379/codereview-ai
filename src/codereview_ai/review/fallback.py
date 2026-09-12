"""审查失败回退网关（自定义实现，非 litellm router fallback）。

`FallbackLLMGateway` 按序尝试多个 `LLMGateway`：前一个抛 `LLMError` 自动切下一个，
全败抛 `LLMError`。接口与 `LLMGateway.complete` 对齐（含 `usage_sink` 透传，记到
实际命中的那台上），`Reviewer` 可直接以本包装替换单网关，reviewer / pipeline 零改动。

现状只有「审查」在真实消费模型，回退链接这条链路；将来若新增别的模型调用阶段，
可复用本类。凭据优先级与 `apply_env_replay` 一致：host 已显式配置的同名环境变量
（`{provider}_api_key` / `api_base`）压过 DB 值，密钥压不住。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from codereview_ai.review.llm_gateway import LLMError, LLMGateway


class FallbackLLMGateway:
    """按序尝试多个 LLMGateway：前一个抛 LLMError 自动切下一个，全败抛 LLMError。"""

    def __init__(self, gateways: list[LLMGateway]) -> None:
        self._gateways = list(gateways)

    @property
    def model(self) -> str:
        # 兼容 main.py 启动日志 reviewer.gateway.model（取链首，接口与 LLMGateway 对齐）
        return self._gateways[0].model if self._gateways else ""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        usage_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        last: LLMError | None = None
        for gateway in self._gateways:
            try:
                return await gateway.complete(messages, usage_sink=usage_sink)
            except LLMError as exc:  # 单机失败 → 记下原因，切下一台
                last = exc
                continue
        raise LLMError(f"所有回退模型均失败: {last}") from last


def _env_api_key(llm: object) -> str | None:
    """host override 优先：env 已设 `{provider}_api_key` 用之，否则回退 DB 解密值。"""
    provider = getattr(llm, "provider", "") or ""
    if provider:
        override = os.environ.get(f"{provider}_api_key")
        if override:
            return override
    return (getattr(llm, "api_key", "") or "") or None


def _env_base_url(llm: object) -> str | None:
    override = os.environ.get("api_base")
    if override:
        return override
    return (getattr(llm, "base_url", "") or "") or None


def wrap_fallback(
    llms: Iterable[object], *, backend: object | None = None
) -> LLMGateway | FallbackLLMGateway:
    """把链上每个解析模型构造成独立 LLMGateway；单个直接返回，多个包 `FallbackLLMGateway`。"""
    gateways: list[LLMGateway] = []
    for llm in llms:
        gateways.append(
            LLMGateway(
                model=(getattr(llm, "model", "") or getattr(llm, "name", "")),
                backend=backend,  # type: ignore[arg-type]
                max_tokens=getattr(llm, "max_tokens", None),
                temperature=getattr(llm, "temperature", None),
                api_key=_env_api_key(llm),
                base_url=_env_base_url(llm),
            )
        )
    return gateways[0] if len(gateways) == 1 else FallbackLLMGateway(gateways)

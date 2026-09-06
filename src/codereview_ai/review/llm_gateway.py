"""LLM 网关（DESIGN §10/§7.3）：LiteLLM 薄封装 + 结构化输出修复链。

- 传输层：`LLMGateway.complete()` —— 只关心「拿到文本内容」或「抛 `LLMError`」，
  绝不在失败时返回错误字符串。backend 可注入（测试用 fake，零网络、零 token）。
- 结构化层（纯函数、无依赖、离线可测）：
  - `repair_json()` —— F2.19 输出修复链：剥围栏 → 去控制符 → 多行值转块标量 →
    截到末个合法元素，逐层尝试，最后产出 dict 或 None。
  - `parse_review_json()` —— 修复 + 校验 + 归一化，产出 `ReviewResult`；
    category/severity 非法值降级到 `other`/`low`，而不是丢弃整个 finding。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from codereview_ai.domain.models import Category, Finding, ReviewResult, ReviewScores, Severity


class LLMError(RuntimeError):
    """所有 LLM/接入层失败统一抛这个异常，由 pipeline 捕获决定降级/失败。"""


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------

#: backend: async (messages) -> 文本内容。默认走 LiteLLM（惰性导入，离线可注入 fake）。
Backend = Callable[[list[dict[str, Any]]], Awaitable[str]]


class LLMGateway:
    def __init__(
        self,
        *,
        model: str,
        backend: Backend | None = None,
        json_object: bool = False,
    ) -> None:
        self.model = model
        self.json_object = json_object
        self._backend = backend or self._litellm_backend

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        """发起点请求并返回文本。任何失败（含空返回）都抛 LLMError。"""
        try:
            text = await self._backend(messages)
        except LLMError:
            raise
        except Exception as exc:  # 网络/超时/鉴权等一律归一为 LLMError
            raise LLMError(f"LLM call failed: {exc}") from exc
        if not text or not text.strip():
            raise LLMError("LLM returned empty content")
        return text

    async def _litellm_backend(self, messages: list[dict[str, Any]]) -> str:
        try:
            import litellm
        except ImportError as exc:  # 未安装 litellm 却无注入 backend → 明确报错
            raise LLMError("litellm not installed but no backend injected") from exc
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.json_object:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await litellm.acompletion(**kwargs)
        content = resp.choices[0].message.content
        return content or ""


# ---------------------------------------------------------------------------
# 结构化输出修复链（纯函数）
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*$", re.M)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _loads(s: str) -> dict[str, Any] | None:
    """json.loads 但类型安全：成功返回 dict，失败返回 None（绝不抛）。"""
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
# "key: 值" 改写成引号键（针对 LLM 爱输出无引号键的坏 JSON）。
# 负向断言确保 key 前不是引号/字母/数字/下划线——即必须是 {  ,  : 空白或行首，
# 且不会误改已带引号的键或标识符尾部。
_KEY_RE = re.compile(r"(?<![\"'A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _scrub_control_chars(text: str) -> str:
    return _CONTROL_CHAR_RE.sub("", text)


def _truncate_to_last_object(text: str) -> str:
    """截到最后一个 `}`（含），用于模型未闭合 / 提前截断的场景。"""
    close = text.rfind("}")
    return text[: close + 1] if close >= 0 else text


def repair_json(raw: str) -> dict[str, Any] | None:
    """按层修复 LLM 输出的 JSON，逐层尝试，成功即返回 dict，否则 None。

    顺序（F2.19）：剥代码围栏 → 去控制符 → 多行未加引号键转块标量 →
    截到末个合法元素。某层没帮上忙就继续下一层；全部失败返回 None。
    """
    candidates = [raw]
    fence = _strip_fences(raw)
    if fence != raw:
        candidates.append(fence)

    for base in list(candidates):
        for variant in (base, _scrub_control_chars(base)):
            stripped = variant.strip()
            if not stripped:
                continue
            # 1) 直接解析
            if (obj := _loads(stripped)) is not None:
                return obj
            # 2) 去尾逗号
            if (obj := _loads(_TRAILING_COMMA_RE.sub(r"\1", stripped))) is not None:
                return obj
            # 3) 修未加引号键
            if (obj := _loads(_KEY_RE.sub(lambda m: f'"{m.group(1)}":', stripped))) is not None:
                return obj
            # 4) 截到末个 `}`（对象已闭合但尾部残留文本 / 多输出）
            if (obj := _loads(_truncate_to_last_object(stripped))) is not None:
                return obj
    return None


# ---------------------------------------------------------------------------
# 结构化校验 + 归一化
# ---------------------------------------------------------------------------

_SCORE_MAX = {
    "correctness": 40,
    "security": 30,
    "practices": 20,
    "performance": 5,
    "commit_quality": 5,
}
_CATEGORIES = {c.value: c for c in Category}
_SEVERITIES = {s.value: s for s in Severity}


def _clamp_scores(scores: dict[str, Any]) -> ReviewScores:
    out: dict[str, int] = {}
    for name, cap in _SCORE_MAX.items():
        val = scores.get(name, 0)
        out[name] = max(0, min(int(val or 0), cap))
    return ReviewScores(**out)


def _coerce_finding(item: Any) -> Finding | None:
    if not isinstance(item, dict):
        return None
    content = str(item.get("content") or "").strip()
    file = str(item.get("file") or "").strip()
    if not content or not file:
        return None
    cat_raw = item.get("category")
    cat = _CATEGORIES.get(cat_raw) if isinstance(cat_raw, str) else Category.OTHER
    if cat is None:
        cat = Category.OTHER
    sev_raw = item.get("severity")
    sev = _SEVERITIES.get(sev_raw) if isinstance(sev_raw, str) else Severity.LOW
    if sev is None:
        sev = Severity.LOW
    return Finding(
        content=content,
        category=cat,
        severity=sev,
        file=file,
        existing_code=str(item.get("existing_code") or "").strip(),
        suggestion_code=(
            str(item["suggestion_code"]).strip() if item.get("suggestion_code") else None
        ),
        thinking=str(item.get("thinking") or "").strip() or None,
    )


def parse_review_json(raw: str | dict[str, Any]) -> ReviewResult:
    """把 LLM 文本（或已修复的 dict）解析成干净的 `ReviewResult`。

    任何入参无法解析时抛 `LLMError`（严格：坏结构视为失败，交由 pipeline 决定
    是否「仅一次」重试 / 降级 / 任务 failed，绝不返回半成品冒充成功）。
    """
    if isinstance(raw, dict):
        return _build_review_result(raw)
    repaired = repair_json(raw)
    if not isinstance(repaired, dict):
        raise LLMError("LLM output is not a decodable JSON object")
    return _build_review_result(repaired)


def _build_review_result(data: dict[str, Any]) -> ReviewResult:
    findings: list[Finding] = []
    raw_findings = data.get("findings")
    if isinstance(raw_findings, list):
        for item in raw_findings:
            f = _coerce_finding(item)
            if f is not None:
                findings.append(f)
    scores_raw = data.get("scores")
    scores = _clamp_scores(scores_raw) if isinstance(scores_raw, dict) else ReviewScores()
    skipped = data.get("skipped_files")
    return ReviewResult(
        summary=str(data.get("summary") or "").strip(),
        scores=scores,
        findings=findings,
        skipped_files=[str(s) for s in skipped] if isinstance(skipped, list) else [],
        raw_llm_json=data,
    )

"""agentic 原始 LLM 对话采集（对齐 OCR `session/persist.go` 的逐条事件流）。

- `ConversationRecorder`：按 `task_id` 逐条把一次 `llm.chat()/summarize()` 的
  request(messages)+response(content/tool_calls/usage) 即时落 `review_conversation`。
  写入异常一律吞掉记 warning——对话落库**绝不阻断审查主链**（护栏）。
- contextvar 注入采集器（仿 `logging.TRACE_ID` 模式），`llm_adapter`/`sandbox` 只读
  不传参，故不改 `llm_factory`/`ToolCallingLLM` 签名：
  - `ACTIVE_RECORDER`：worker 在 `_do_review_pull_request` 内 set；adapter 检测到非
    None 即采集。
  - `ACTIVE_PHASE`：sandbox 在每个阶段入口 set（plan/main/re_location/review_filter/
    scoring），后台压缩轮默认 `"loop"`。
- `seq` 每 task 单调递增；组并发下按插入序交错（gather），展示按 id 排序即真实时序。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ModelUsage, ReviewConversation

logger = logging.getLogger("codereview_ai.agentic.capture")


def _usage_dict(response: dict[str, Any] | None) -> dict[str, int] | None:
    """从 LLM response 里宽松取 usage（prompt/completion/total tokens）。"""
    if not isinstance(response, dict):
        return None
    u = response.get("usage")
    if not isinstance(u, dict):
        return None
    try:
        return {
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
        }
    except (TypeError, ValueError):
        return None


def _llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按模型定价估算单次调用成本（LiteLLM 计费表）；查不到/失败降级为 0.0。

    成本仅用于看板归因，任何异常都不阻断（与对话落库同样的护栏语义）。
    定价表 key 常带 provider 前缀（如 `openrouter/deepseek/deepseek-v4-flash`），
    而业务侧模型名可能只有 `deepseek/deepseek-v4-flash` → 按「最末段模型名」后缀匹配，
    命中第一个候选即用其每 token 单价。
    """
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0.0
    try:
        import litellm

        table = getattr(litellm, "model_cost", None) or {}
        suffix = f"/{model.rsplit('/', 1)[-1].lower()}"
        entry = next(
            (v for k, v in table.items() if k.lower().endswith(suffix)),
            None,
        )
        if entry is None:
            return 0.0
        p_in = float(entry.get("input_cost_per_token") or 0.0)
        p_out = float(entry.get("output_cost_per_token") or 0.0)
        return round(prompt_tokens * p_in + completion_tokens * p_out, 6)
    except Exception:  # noqa: BLE001 —— 成本估算失败不影响审查与用量记录
        return 0.0

#: 当前生效的对话采集器（None = 不采集，如离线单测/FakeRuntime 路径）。
ACTIVE_RECORDER: contextvars.ContextVar[ConversationRecorder | None] = contextvars.ContextVar(
    "active_conversation_recorder", default=None
)
#: 当前阶段标记；未 set 的调用（如后台压缩轮）落到 "loop"。
ACTIVE_PHASE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_conversation_phase", default="loop"
)


def diff_usage_sink(engine: AsyncEngine, task_id: int) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """diff 审查的 `ModelUsage` 落库 sink：gateway 每条 LLM 调用回调它写入用量行。

    agent 模式走 `ConversationRecorder.record` 进同一张表；此处补上 diff（走普通
    `gateway.complete`、不产对话）的用量，让看板 Token/成本能按实际模式拆分。
    落库失败与外抛无关，一律吞掉记 warning（与对话落库同护栏，不阻断审查主链）。
    """

    async def sink(usage: dict[str, Any]) -> None:
        model = (usage.get("model") or "")[:128]
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        session = session_factory(engine)
        async with session() as s:
            try:
                s.add(ModelUsage(
                    task_id=task_id,
                    model=model,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=int(usage.get("total_tokens") or 0),
                    cost=_llm_cost(model, prompt, completion),
                ))
                await s.commit()
            except Exception as exc:  # noqa: BLE001 —— 用量落库失败不阻断审查
                await s.rollback()
                logger.warning("diff 用量落库失败（task#%s）：%s", task_id, exc)

    return sink


@asynccontextmanager
async def conversation_capture(recorder: ConversationRecorder | None):
    """在作用域内开启对话采集，退出时还原（仿 `logging.trace` 还原令牌）。"""
    if recorder is None:
        yield
        return
    token = ACTIVE_RECORDER.set(recorder)
    try:
        yield
    finally:
        ACTIVE_RECORDER.reset(token)


def set_phase(phase: str):
    """设置当前阶段标记；返回还原回调（sandbox 阶段进出成对使用）。"""
    token = ACTIVE_PHASE.set(phase)
    return lambda: ACTIVE_PHASE.reset(token)


def _dumps(value: Any) -> str:
    """宽松序列化 request/response；失败返回空串（不阻断采集）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


class ConversationRecorder:
    """按 task 逐条写 `review_conversation` 的采集器（单行 INSERT+commit，即时落库）。"""

    def __init__(self, engine: AsyncEngine, *, task_id: int | None, trace_id: str = "") -> None:
        self._engine = engine
        self._task_id = task_id
        self._trace_id = trace_id
        self._seq = 0
        self._tool_calls = 0
        self._lock = asyncio.Lock()

    def metrics(self) -> tuple[int, int]:
        """已采集的 (对话轮数, 工具调用数)；供任务收尾写 `review_task.chat_rounds/tool_calls`。

        一次性内存累计，无需重查/重解析落库 JSON。
        """
        return self._seq, self._tool_calls

    async def record(
        self,
        phase: str,
        *,
        request: list[dict[str, Any]] | None,
        response: dict[str, Any] | None,
        model: str = "",
    ) -> None:
        """落一条对话记录；任何写入异常吞掉记 warning（本质上是审计/诊断，不降级）。"""
        if self._task_id is None:
            return
        async with self._lock:
            self._seq += 1
            seq = self._seq
            self._tool_calls += len((response or {}).get("tool_calls") or [])
        session = session_factory(self._engine)
        async with session() as s:
            try:
                s.add(ReviewConversation(
                    task_id=self._task_id,
                    seq=seq,
                    phase=(phase or "loop")[:32],
                    model=(model or "")[:64],
                    trace_id=(self._trace_id or "")[:64],
                    request_json=_dumps(request),
                    response_json=_dumps(response),
                ))
                # 同步落用量行（看板成本/Token 归因底座，DESIGN §10）：usage 缺失
                # 或写入失败都不影响对话采集主链（与对话落库同样吞异常记 warning）。
                usage = _usage_dict(response)
                if usage is not None:
                    s.add(ModelUsage(
                        task_id=self._task_id,
                        phase=(phase or "loop")[:32],
                        model=(model or "")[:128],
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        total_tokens=usage["total_tokens"],
                        cost=_llm_cost(model, usage["prompt_tokens"], usage["completion_tokens"]),
                    ))
                await s.commit()
            except Exception as exc:  # noqa: BLE001 —— 对话落库失败不外抛，不阻断审查
                await s.rollback()
                logger.warning("对话落库失败（task#%s seq=%d）：%s", self._task_id, seq, exc)

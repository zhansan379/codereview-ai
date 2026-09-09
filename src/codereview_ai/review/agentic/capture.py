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
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ReviewConversation

logger = logging.getLogger("codereview_ai.agentic.capture")

#: 当前生效的对话采集器（None = 不采集，如离线单测/FakeRuntime 路径）。
ACTIVE_RECORDER: contextvars.ContextVar[ConversationRecorder | None] = contextvars.ContextVar(
    "active_conversation_recorder", default=None
)
#: 当前阶段标记；未 set 的调用（如后台压缩轮）落到 "loop"。
ACTIVE_PHASE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_conversation_phase", default="loop"
)


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
                await s.commit()
            except Exception as exc:  # noqa: BLE001 —— 对话落库失败不外抛，不阻断审查
                await s.rollback()
                logger.warning("对话落库失败（task#%s seq=%d）：%s", self._task_id, seq, exc)

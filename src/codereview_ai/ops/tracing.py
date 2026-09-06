"""trace_id 贯穿中间件（DESIGN §15.1）。

读取 `X-Trace-Id` 请求头，或为 webhook 入口生成 `webhook-<hex>`；把 id 写入
`logging.TRACE_ID`（contextvar，异步链全程可见），并在响应头回写同一 id，
便于用户拿它检索一次审查的完整链路。
"""

from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from codereview_ai.logging import TRACE_ID

X_TRACE_ID = "X-Trace-Id"


class TraceMiddleware(BaseHTTPMiddleware):
    """每个请求：生成/继承 trace_id → 设置 ContextVar → 响应头回写。"""

    async def dispatch(self, request: Request, call_next):
        tid = request.headers.get(X_TRACE_ID) or f"webhook-{uuid4().hex[:12]}"
        token = TRACE_ID.set(tid)
        try:
            response = await call_next(request)
            response.headers[X_TRACE_ID] = tid
            return response
        finally:
            TRACE_ID.reset(token)

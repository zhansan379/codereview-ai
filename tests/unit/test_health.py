"""ops: health/ready 端点与 trace 中间件测试。"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import Request

import codereview_ai.ops.health as health_mod
from codereview_ai.logging import TRACE_ID
from codereview_ai.ops.health import router as health_router
from codereview_ai.ops.tracing import X_TRACE_ID, TraceMiddleware


def _make_app(engine: AsyncEngine | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(health_router)
    if engine is not None:
        app.state.engine = engine
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_health_returns_ok():
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


async def test_ready_without_engine_is_not_configured():
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "not-configured"


async def test_ready_with_live_engine_is_ok():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    app = _make_app(engine)
    async with await _client(app) as c:
        r = await c.get("/ready")
        assert r.status_code == 200
        assert r.json()["checks"]["db"] == "ok"
    await engine.dispose()


async def test_ready_with_unreachable_db_returns_503(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    app = _make_app(engine)

    async def _broken(_engine):
        return False

    monkeypatch.setattr(health_mod, "_db_check", _broken)
    async with await _client(app) as c:
        r = await c.get("/ready")
        assert r.status_code == 503


async def test_trace_middleware_echoes_provided_id():
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/health", headers={X_TRACE_ID: "from-client-1"})
        assert r.headers[X_TRACE_ID] == "from-client-1"


async def test_trace_middleware_generates_id():
    app = _make_app()
    async with await _client(app) as c:
        r = await c.get("/health")
        tid = r.headers[X_TRACE_ID]
        assert tid.startswith("webhook-")


async def test_trace_contextvar_is_set_for_each_request():
    app = _make_app()

    async def handler_route(request: Request) -> dict[str, str]:
        return {"in_context": TRACE_ID.get()}

    app.add_api_route("/echo-context", handler_route, methods=["GET"])

    async with await _client(app) as c:
        r1 = await c.get("/echo-context", headers={X_TRACE_ID: "aaa"})
        assert r1.json()["in_context"] == "aaa"
        r2 = await c.get("/echo-context", headers={X_TRACE_ID: "bbb"})
        assert r2.json()["in_context"] == "bbb"

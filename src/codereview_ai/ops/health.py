"""健康检查端点（DESIGN §15.3）。

- `GET /health`：存活，恒 200。
- `GET /ready`：依赖就绪——DB 引擎可连接则 true，否则 503。
  DB 未配置（app 未设置 `state.engine`）视为"未配置"而非故障，便于无存储的冒烟。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _db_check(engine: Any) -> bool:
    """对 engine 做一次 `SELECT 1` 连通性探测。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, str | bool] = {}

    engine = getattr(state, "engine", None)
    if engine is None:
        checks["db"] = "not-configured"
    elif await _db_check(engine):
        checks["db"] = "ok"
    else:
        checks["db"] = "unreachable"

    queue_ready: Callable[[], Awaitable[bool]] | None = getattr(state, "queue_ready", None)
    if queue_ready is None:
        checks["queue"] = "not-configured"
    elif await queue_ready():
        checks["queue"] = "ok"
    else:
        checks["queue"] = "error"

    ok = all(str(v) in ("ok", "not-configured") for v in checks.values())
    if not ok:
        return JSONResponse(status_code=503, content={"status": "not-ready", "checks": checks})
    return JSONResponse(content={"status": "ok", "checks": checks})

"""Webhook 入口（DESIGN §9 webhook 路由）。

职责边界：来源识别 → 签名校验 → 投递队列。校验通过即返回 202。**不做**
事件解析/过滤/审查编排——那些在 worker（queue）与 forge 适配层（DESIGN §7）。

依赖注入：从 `request.app.state` 读取
- `settings`：取 `webhook_secret`
- `enqueuer`：`async enqueue(provider, raw_body)` 投递原始事件
（main 装配时以真实 queue 注入；测试注入 fake）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette import status

from codereview_ai.forges.signatures import detect_forge, verify_signature

router = APIRouter()


@router.post("/webhook")
async def webhook_entry(request: Request) -> JSONResponse:
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "empty body")

    provider = detect_forge(request.headers)
    settings: Any = request.app.state.settings
    secret = getattr(settings, "webhook_secret", "") or ""
    if not secret:
        raise HTTPException(500, "webhook_secret not configured")
    if not verify_signature(provider, secret, dict(request.headers), raw):
        raise HTTPException(401, "invalid signature")

    enqueuer = getattr(request.app.state, "enqueuer", None)
    if enqueuer is None:
        raise HTTPException(503, "enqueuer not ready")
    await enqueuer.enqueue(provider, raw)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "accepted", "provider": provider},
    )

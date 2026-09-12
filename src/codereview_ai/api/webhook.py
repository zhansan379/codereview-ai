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
from starlette.middleware.base import BaseHTTPMiddleware

from codereview_ai.forges.signatures import detect_forge, verify_signature

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Webhook 配置错误检测与持久化提醒
# ─────────────────────────────────────────────────────────────────────────────


def _has_webhook_headers(headers: dict[str, str]) -> bool:
    """检测请求是否带有 webhook 相关的 headers（说明是平台发来的 webhook 请求）。"""
    lowered = {k.lower(): v for k, v in headers.items()}
    webhook_headers = {
        "x-github-event",
        "x-gitea-event",
        "x-gitee-event",
        "x-gitee-token",
        "x-gitlab-token",
        "x-hub-signature-256",
        "x-gitea-signature",
    }
    return any(h in lowered for h in webhook_headers)


class WebhookHelpMiddleware(BaseHTTPMiddleware):
    """中间件：检测 webhook 路径配置错误并落库，供前端轮询展示持久化提醒。

    当请求满足以下条件时，记录错误到数据库：
    1. 请求方法是 POST
    2. 请求路径不是 /webhook
    3. 请求带有 webhook 相关的 headers（说明是平台发来的 webhook 请求）
    """

    async def dispatch(self, request: Request, call_next):
        # 只拦截 POST 请求
        if request.method != "POST":
            return await call_next(request)

        # 只拦截非 /webhook 路径
        if request.url.path == "/webhook":
            return await call_next(request)

        # 检测是否有 webhook headers
        if not _has_webhook_headers(dict(request.headers)):
            return await call_next(request)

        # 落库：持久化错误记录，供前端轮询展示
        provider = detect_forge(request.headers)
        host = request.headers.get("host", "你的服务地址")
        scheme = request.url.scheme
        wrong_url = f"{scheme}://{host}{request.url.path}"
        correct_url = f"{scheme}://{host}/webhook"
        await self._persist_error(request, provider, wrong_url, correct_url)

        # 返回标准 404（不再返回 HTML 提示页）
        return await call_next(request)

    async def _persist_error(
        self, request: Request, provider: str, wrong_url: str, correct_url: str
    ) -> None:
        """将错误记录落库（异步，不阻塞响应）。"""
        try:
            engine = getattr(request.app.state, "engine", None)
            if engine is None:
                import logging
                logging.getLogger("codereview_ai.webhook").warning(
                    "webhook 错误落库跳过：engine 未初始化"
                )
                return
            from codereview_ai.storage.db import session_factory
            from codereview_ai.storage.webhook_error_repo import WebhookErrorRepository

            # 提取来源 IP
            source_ip = request.client.host if request.client else ""

            async with session_factory(engine)() as session:
                repo = WebhookErrorRepository(session)
                await repo.create(
                    provider=provider or "unknown",
                    wrong_url=wrong_url,
                    correct_url=correct_url,
                    source_ip=source_ip,
                )
                await session.commit()
                import logging
                logging.getLogger("codereview_ai.webhook").info(
                    "webhook 配置错误已落库：provider=%s wrong_url=%s", provider, wrong_url
                )
        except Exception as exc:
            # 落库失败不影响主流程，但记录日志便于排查
            import logging
            logging.getLogger("codereview_ai.webhook").error(
                "webhook 错误落库失败：%s", exc
            )


# ─────────────────────────────────────────────────────────────────────────────
# 正式 webhook 入口
# ─────────────────────────────────────────────────────────────────────────────


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

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
from fastapi.responses import HTMLResponse, JSONResponse
from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware

from codereview_ai.forges.signatures import detect_forge, verify_signature

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# 兜底提示页：当 webhook URL 缺少 /webhook 路径时，友好提示用户
# ─────────────────────────────────────────────────────────────────────────────
_WEBHOOK_HELP_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Webhook 配置提示</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            max-width: 800px;
            margin: 60px auto;
            padding: 20px;
            line-height: 1.6;
            color: #333;
        }}
        .alert {{
            background: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }}
        .alert h2 {{
            margin-top: 0;
            color: #856404;
        }}
        code {{
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', monospace;
            color: #d63384;
        }}
        .correct-url {{
            background: #d1ecf1;
            border: 1px solid #17a2b8;
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            font-size: 16px;
        }}
        .wrong-url {{
            background: #f8d7da;
            border: 1px solid #f5c6cb;
            border-radius: 8px;
            padding: 15px;
            margin: 15px 0;
            text-decoration: line-through;
            color: #721c24;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }}
        th {{
            background: #f8f9fa;
        }}
        .note {{
            background: #e7f3ff;
            border-left: 4px solid #2196f3;
            padding: 15px;
            margin: 20px 0;
        }}
    </style>
</head>
<body>
    <div class="alert">
        <h2>⚠️ Webhook 路径配置错误</h2>
        <p>检测到来自 <strong>{provider}</strong> 的 webhook 请求，但 URL 路径不正确。</p>
    </div>

    <div class="wrong-url">
        ❌ 当前配置（错误）：<code>{wrong_url}</code>
    </div>

    <div class="correct-url">
        ✅ 正确配置：<code>{correct_url}/webhook</code>
    </div>

    <h3>各平台 Webhook 配置对照表</h3>
    <table>
        <tr>
            <th>平台</th>
            <th>Webhook URL</th>
        </tr>
        <tr>
            <td>GitHub</td>
            <td><code>https://你的服务地址/webhook</code></td>
        </tr>
        <tr>
            <td>GitLab</td>
            <td><code>https://你的服务地址/webhook</code></td>
        </tr>
        <tr>
            <td>Gitee</td>
            <td><code>https://你的服务地址/webhook</code></td>
        </tr>
        <tr>
            <td>Gitea</td>
            <td><code>https://你的服务地址/webhook</code></td>
        </tr>
    </table>

    <h3>配置步骤</h3>
    <ol>
        <li>进入代码仓库的 <strong>管理 → Webhooks</strong> 页面</li>
        <li>找到刚才添加的 webhook，点击「修改」</li>
        <li>将 URL 末尾加上 <code>/webhook</code></li>
        <li>保存后点击「测试」验证</li>
    </ol>

    <div class="note">
        <strong>提示：</strong>确保 WebHook 密码/Secret 与服务端环境变量
        <code>CR_WEBHOOK_SECRET</code> 保持一致，否则会返回 401 签名错误。
    </div>
</body>
</html>
"""


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
    """中间件：检测 webhook 路径配置错误并返回友好提示页。

    当请求满足以下条件时，返回提示页面：
    1. 请求方法是 POST
    2. 请求路径不是 /webhook
    3. 请求带有 webhook 相关的 headers（说明是平台发来的 webhook 请求）

    同时会将错误记录落库，供前端轮询展示持久化提醒。
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

        # 返回友好提示页面
        provider = detect_forge(request.headers)
        host = request.headers.get("host", "你的服务地址")
        scheme = request.url.scheme
        wrong_url = f"{scheme}://{host}{request.url.path}"
        correct_url = f"{scheme}://{host}/webhook"

        # 落库：持久化错误记录，供前端轮询展示
        await self._persist_error(request, provider, wrong_url, correct_url)

        html = _WEBHOOK_HELP_HTML.format(
            provider=provider.upper() if provider else "代码托管平台",
            wrong_url=wrong_url,
            correct_url=f"{scheme}://{host}",
        )
        return HTMLResponse(content=html, status_code=404)

    async def _persist_error(
        self, request: Request, provider: str, wrong_url: str, correct_url: str
    ) -> None:
        """将错误记录落库（异步，不阻塞响应）。"""
        try:
            engine = getattr(request.app.state, "engine", None)
            if engine is None:
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
        except Exception:
            # 落库失败不影响主流程，静默忽略
            pass


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

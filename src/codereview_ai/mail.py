"""阶段 D 邮箱验证外发：best-effort SMTP 发送，未配置 `smtp_host` 时优雅降级。

不发异常、不阻塞注册：任何失败仅记 warning 返回 False。测试以 monkeypatch `send_verification_email`
捕获 token/link，零网络依赖。
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)


def send_verification_email(
    settings: Any, *, to_email: str, username: str, token: str, base_url: str = ""
) -> bool:
    """发送邮箱验证链接（含一次性 JWT `token`）。SMTP 未配置或发送失败 → False，不抛异常。"""
    host = getattr(settings, "smtp_host", "")
    port = int(getattr(settings, "smtp_port", 587))
    from_addr = getattr(settings, "smtp_from", "")
    user = getattr(settings, "smtp_user", "")
    password = getattr(settings, "smtp_password", "")
    starttls = bool(getattr(settings, "smtp_starttls", True))

    if not (host and from_addr):
        logger.warning("smtp_host/from 未配置，跳过邮件发送（to=%s）", to_email)
        return False
    if base_url:
        # 有前端 SPA 根地址 → 指向注册页面的邮箱验证路由，用户体验一致
        link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    else:
        # 未配 → 指回后端 API 端点（浏览器打开返回 JSON，功能可用）
        link = f"/api/auth/verify-email?token={token}"
    msg = EmailMessage()
    msg["Subject"] = "验证你的 CodeReview AI 邮箱"
    msg["From"] = from_addr
    msg["To"] = to_email
    msg.set_content(
        f"你好 {username}，\n\n请点击以下链接验证你的邮箱（24 小时内有效）：\n\n{link}\n\n"
        "若非本人操作可忽略此邮件。"
    )
    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            if starttls:
                server.starttls()
            if user:
                server.login(user, password)
            server.send_message(msg)
    except Exception:  # 网络/认证失败一律不阻塞注册
        logger.warning("验证邮件发送失败（to=%s）", to_email, exc_info=True)
        return False
    return True

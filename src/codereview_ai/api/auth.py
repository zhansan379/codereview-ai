"""后台登录（DESIGN §14.3，F5.1 单用户 JWT）。

`POST /api/auth/login`：校验 admin 口令（常量时间比较）→ 签发 HS256 JWT。
admin 口令来自环境变量 `CR_ADMIN_PASSWORD`，未配置则 `Settings` 启动 fail-fast，
故这里读到的一定非空。
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette import status

#: token 有效期（秒）
TOKEN_TTL_SECONDS = 12 * 3600

router = APIRouter()


class LoginRequest(BaseModel):
    password: str = ""


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = TOKEN_TTL_SECONDS
    user: str = "admin"


def issue_token(secret_key: str, *, sub: str = "admin", ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发 HS256 JWT。`secret_key` 来自 Settings（无默认，启动已校验）。"""
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    settings: Any = request.app.state.settings
    admin_password = getattr(settings, "admin_password", "") or ""
    if not admin_password:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "admin_password 未配置")
    if not hmac.compare_digest(body.password.encode("utf-8"), admin_password.encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "口令错误")
    return LoginResponse(access_token=issue_token(settings.secret_key))

"""后台登录（DESIGN §14.3，F5.1→F5.11 多用户 RBAC）。

`POST /api/auth/login`：按 `username` 查用户 → `verify_password`（scrypt 常时比较）
→ 校验 `enabled` → 签发 JWT（`sub`=用户 id，`LoginResponse` 附带 user+permissions）。
`GET /api/auth/me`：返回当前用户与权限集（前端硬刷新后重同步用）。
首个 admin 由 `seed_rbac` 从 `CR_ADMIN_PASSWORD` 派生。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette import status

from codereview_ai.api.deps import CurrentUser, get_db, resolved_permission_codes
from codereview_ai.security import verify_password
from codereview_ai.storage.models import Role, User

#: token 有效期（秒）
TOKEN_TTL_SECONDS = 12 * 3600

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str = ""
    enabled: bool = True
    role_id: int
    role_name: str = ""


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = TOKEN_TTL_SECONDS
    user: UserOut
    permissions: list[str]


class MeResponse(BaseModel):
    user: UserOut
    permissions: list[str]


def issue_token(secret_key: str, *, sub: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发 HS256 JWT。`sub` 为**用户 id**（username 可改名，id 稳定）。"""
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        enabled=user.enabled, role_id=user.role_id, role_name=user.role.name,
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)
) -> LoginResponse:
    user = (await session.execute(
        select(User)
        .where(User.username == body.username.strip())
        .options(selectinload(User.role).selectinload(Role.permissions))
    )).scalar_one_or_none()
    if user is None or not user.enabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或口令错误")
    settings: Any = request.app.state.settings
    secret_key = getattr(settings, "secret_key", "") or ""
    return LoginResponse(
        access_token=issue_token(secret_key, sub=str(user.id)),
        user=_to_out(user),
        permissions=sorted(resolved_permission_codes(user)),
    )


@router.get("/auth/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    """返回当前用户 + 权限集（前端硬刷新后用它重同步 sessionStorage）。"""
    return MeResponse(user=_to_out(user), permissions=sorted(resolved_permission_codes(user)))

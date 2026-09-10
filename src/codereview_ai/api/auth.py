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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette import status

from codereview_ai.api.deps import CurrentUser, get_db, resolved_permission_codes
from codereview_ai.security import hash_password, reset_meets_policy, verify_password
from codereview_ai.storage.models import Role, User, Workspace

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


class RegisterRequest(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class RegisterResponse(BaseModel):
    user: UserOut
    workspace: WorkspaceOut


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


def _workspace_slug(username: str) -> str:
    """由用户名派生 slug：非字母数字折叠为 `-`；撞 uniq 的加固在 `_unique_slug`。"""
    base = "".join(ch if ch.isalnum() else "-" for ch in username.lower()).strip("-") or "ws"
    return base


async def _unique_slug(session: AsyncSession, slug: str) -> str:
    """确保 slug 全局唯一（避免不同用户名折叠成同名）。"""
    candidate, n = slug, 1
    while (await session.execute(select(Workspace.id).where(Workspace.slug == candidate))).first():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


@router.post(
    "/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_db)
) -> RegisterResponse:
    """公开自助注册：建 `member` 角色用户 + 其私有 workspace（owner=本人），不签发 token。

    校验用户名非空+唯一、密码过策略（≥8 位）。返回 201 + user/workspace，客户端随后走
    `/auth/login`。仿 `login` 的公开写端点模式（无鉴权依赖）。
    """
    username = body.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名不能为空")
    if not reset_meets_policy(body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 8 位")
    member_role = (await session.execute(
        select(Role).where(Role.builtin_code == "member")
    )).scalar_one_or_none()
    if member_role is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "注册功能尚未初始化（缺 member 角色）"
        )
    display_name = body.display_name.strip() if body.display_name else ""
    user = User(
        username=username,
        password_hash=hash_password(body.password),
        display_name=display_name or username,
        enabled=True,
        role_id=member_role.id,
    )
    session.add(user)
    try:
        await session.flush()  # 先取 user.id 供 workspace.owner_id；唯一用户名冲突在此暴露
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from None
    workspace = Workspace(
        name=display_name or username,
        slug=await _unique_slug(session, _workspace_slug(username)),
        owner_id=user.id,
    )
    session.add(workspace)
    await session.commit()
    return RegisterResponse(
        user=UserOut(
            id=user.id, username=user.username, display_name=user.display_name,
            enabled=user.enabled, role_id=member_role.id, role_name=member_role.name,
        ),
        workspace=WorkspaceOut(id=workspace.id, name=workspace.name, slug=workspace.slug),
    )

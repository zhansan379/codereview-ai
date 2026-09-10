"""后台登录（DESIGN §14.3，F5.1→F5.11 多用户 RBAC）+ 阶段 D 开放安全加固。

`POST /api/auth/login`：按 `username` 查用户 → `verify_password`（scrypt 常时比较）
→ 校验 `enabled` → **限速/验证码**闸门 → 建服务端会话（AuthSession）→ 签发短命 access(token
带 `sub`+`sid`)+refresh。`GET /api/auth/me` 返回用户与权限集。

阶段 D 新增：
- `POST /auth/refresh`：refresh 轮换 + 重新签发 access（旧 refresh 复用 → 401）。
- `POST /auth/logout`：撤销当前 sid 会话（其 access 即刻失效）。
- `POST /auth/captcha`：签发自托管算术题（限速阈值命中后登录需携带答案）。
- `POST /auth/register` 可选 `email`；配置 SMTP 后发验证链接。
- `GET /auth/verify-email?token=`：校验 email-verify JWT → 置 `email_verified=True`。

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
from codereview_ai.mail import send_verification_email
from codereview_ai.security import (
    generate_token,
    hash_password,
    hash_token,
    reset_meets_policy,
    verify_password,
)
from codereview_ai.security_guard import LoginGuard
from codereview_ai.storage.models import AuthSession, Role, User, Workspace

#: 无 Settings 兜底时的 access token 有效期（秒）——通常以 settings.auth_access_ttl_seconds 为准
TOKEN_TTL_SECONDS = 12 * 3600
#: email 验证令牌有效期（秒）= 24h
EMAIL_VERIFY_TTL_SECONDS = 24 * 3600
#: refresh 令牌取随机段的字节数
REFRESH_RAND_BYTES = 32

router = APIRouter()


def _guard(request: Request) -> LoginGuard:
    """取 `app.state.login_guard`（生命周期装配）；缺失时退回兜底实例。"""
    guard = getattr(request.app.state, "login_guard", None)
    return guard if isinstance(guard, LoginGuard) else LoginGuard()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------- request/response 模型 ----------------

class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""
    captcha_id: str = ""
    captcha_answer: str = ""


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str = ""
    email: str = ""
    email_verified: bool = False
    enabled: bool = True
    role_id: int
    role_name: str = ""


class AccessOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = TOKEN_TTL_SECONDS
    refresh_token: str
    refresh_expires_in: int = 0


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = TOKEN_TTL_SECONDS
    refresh_token: str = ""
    refresh_expires_in: int = 0
    user: UserOut
    permissions: list[str]


class RefreshRequest(BaseModel):
    refresh_token: str = ""


class LogoutRequest(BaseModel):
    refresh_token: str = ""


class CaptchaResponse(BaseModel):
    captcha_id: str
    prompt: str


class CaptchaRequiredResponse(BaseModel):
    """限速/验证码闸门命中时随 4xx 返回的提示头。"""

    detail: str
    captcha_required: bool = False


class MeResponse(BaseModel):
    user: UserOut
    permissions: list[str]
    # BYOK 前端入口：该用户作为 owner 的私有 workspace（导航/空间设置页用；超管可无）
    workspace: WorkspaceOut | None = None


class RegisterRequest(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""
    email: str = ""


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class RegisterResponse(BaseModel):
    user: UserOut
    workspace: WorkspaceOut


def issue_token(
    secret_key: str, *, sub: str, ttl: int = TOKEN_TTL_SECONDS, sid: str | None = None
) -> str:
    """签发 HS256 JWT。`sub` 为**用户 id**；接入阶段 D 后 access token 带 `sid`（会话 id）。"""
    payload: dict[str, Any] = {
        "sub": sub,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(seconds=ttl),
    }
    if sid is not None:
        payload["sid"] = sid
    return jwt.encode(payload, secret_key, algorithm="HS256")


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        email=user.email, email_verified=user.email_verified,
        enabled=user.enabled, role_id=user.role_id, role_name=user.role.name,
    )


def _secret(request: Request) -> str:
    settings: Any = request.app.state.settings
    return getattr(settings, "secret_key", "") or ""


def _access_ttl(settings: Any) -> int:
    return int(getattr(settings, "auth_access_ttl_seconds", TOKEN_TTL_SECONDS))


def _refresh_ttl(settings: Any) -> int:
    return int(getattr(settings, "auth_refresh_ttl_seconds", 14 * 86400))


def _new_session(
    session: AsyncSession, settings: Any, user_id: int, ip: str
) -> tuple[str, str, str]:
    """建登录会话，返回 (sid, refresh_token 明文, refresh_hash)；access 端不用与会话。"""
    sid = generate_token(16)
    refresh = generate_token(REFRESH_RAND_BYTES)
    now = datetime.now(UTC)
    session.add(AuthSession(
        id=sid,
        user_id=user_id,
        refresh_hash=hash_token(refresh),
        created_at=now,
        expires_at=now + timedelta(seconds=_refresh_ttl(settings)),
        revoked_at=None,
        user_agent=ip,
    ))
    return sid, refresh, hash_token(refresh)


# ---------------- 认证端点 ----------------

@router.post("/auth/captcha", response_model=CaptchaResponse)
async def captcha(request: Request) -> CaptchaResponse:
    """签发自托管算术题。**仅在登录命中限速阈值后**客户端取题用。"""
    captcha_id, prompt = _guard(request).new_captcha()
    return CaptchaResponse(captcha_id=captcha_id, prompt=prompt)


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    body: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)
) -> LoginResponse:
    guard = _guard(request)
    ip = _client_ip(request)
    username = body.username.strip()

    # —— 闸门 1：IP 限速（速率窗口内计数达上限 → 429）——
    if not guard.check_rate(ip):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")

    if guard.need_captcha(ip, username):
        if not body.captcha_id or not guard.verify_captcha(body.captcha_id, body.captcha_answer):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "请填写算术验证码",
                headers={"X-Captcha-Required": "true"},
            )

    guard.record_attempt(ip, username)
    user = (await session.execute(
        select(User)
        .where(User.username == username)
        .options(selectinload(User.role).selectinload(Role.permissions))
    )).scalar_one_or_none()

    settings: Any = request.app.state.settings
    secret = _secret(request)
    if user is None or not user.enabled or not verify_password(body.password, user.password_hash):
        guard.record_failure(ip, username)
        cap = guard.need_captcha(ip, username)
        detail = "用户名或口令错误"
        exc = HTTPException(status.HTTP_401_UNAUTHORIZED, detail)
        if cap:
            exc.headers = {"X-Captcha-Required": "true"}
        raise exc

    guard.record_success(ip, username)

    # —— 建服务端会话：access 短命 + refresh 轮换即撤销 ——
    sid, refresh, _refresh_hash = _new_session(session, settings, user.id, ip)
    await session.commit()
    return LoginResponse(
        access_token=issue_token(secret, sub=str(user.id), ttl=_access_ttl(settings), sid=sid),
        expires_in=_access_ttl(settings),
        refresh_token=f"{sid}.{refresh}",  # 复合：sid.随机段（sid/随机段均无 `.`，拆分无歧义）
        refresh_expires_in=_refresh_ttl(settings),
        user=_to_out(user),
        permissions=sorted(resolved_permission_codes(user)),
    )


@router.post("/auth/refresh", response_model=LoginResponse)
async def refresh(
    body: RefreshRequest, request: Request, session: AsyncSession = Depends(get_db)
) -> LoginResponse:
    """refresh 轮换：校验旧 refresh → 更新会话 refresh_hash/expiry → 重签 access + 新 refresh。

    旧 refresh 被复用、过期或被撤销 → 401。轮换后旧 access 仍有效至其自身 exp（同 sid 未被撤）。
    """
    if not body.refresh_token or "." not in body.refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的刷新令牌")
    sid, rand = body.refresh_token.split(".", 1)
    row = (await session.execute(
        select(AuthSession).where(AuthSession.id == sid)
    )).scalar_one_or_none()
    # SQLite 读回 timezone 列是 naive（不保 tz），故以 naive UTC 比较
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC).replace(tzinfo=None)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌已失效")
    if hash_token(rand) != row.refresh_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "刷新令牌已失效")
    user = (await session.execute(
        select(User)
        .where(User.id == row.user_id)
        .options(selectinload(User.role).selectinload(Role.permissions))
    )).scalar_one_or_none()
    if user is None or not user.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不可用")

    # 轮换：新随机段、延长会话
    new_rand = generate_token(REFRESH_RAND_BYTES)
    settings: Any = request.app.state.settings
    row.refresh_hash = hash_token(new_rand)
    row.expires_at = datetime.now(UTC) + timedelta(seconds=_refresh_ttl(settings))
    await session.commit()

    secret = _secret(request)
    return LoginResponse(
        access_token=issue_token(secret, sub=str(user.id), ttl=_access_ttl(settings), sid=row.id),
        expires_in=_access_ttl(settings),
        refresh_token=f"{row.id}.{new_rand}",
        refresh_expires_in=_refresh_ttl(settings),
        user=_to_out(user),
        permissions=sorted(resolved_permission_codes(user)),
    )


@router.post("/auth/logout")
async def logout(
    body: LogoutRequest, request: Request, session: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    """撤销当前会话（其 access 即刻失效）。无论令牌是否有效都返回 ok=True（幂等）。"""
    if body.refresh_token and "." in body.refresh_token:
        sid = body.refresh_token.split(".", 1)[0]
        row = (await session.execute(
            select(AuthSession).where(AuthSession.id == sid)
        )).scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await session.commit()
    return {"ok": True}


@router.get("/auth/me", response_model=MeResponse)
async def me(user: CurrentUser, session: AsyncSession = Depends(get_db)) -> MeResponse:
    """返回当前用户 + 权限集（前端硬刷新后用它重同步 sessionStorage）。

    BYOK：附带 `workspace`（该用户作为 owner 的私有空间，供前端空间设置入口；非 owner
    /超管为 None）。
    """
    workspace: WorkspaceOut | None = None
    owned = (await session.execute(
        select(Workspace).where(Workspace.owner_id == user.id),
    )).scalar_one_or_none()
    if owned is not None:
        workspace = WorkspaceOut(id=owned.id, name=owned.name, slug=owned.slug)
    return MeResponse(
        user=_to_out(user), permissions=sorted(resolved_permission_codes(user)),
        workspace=workspace,
    )


# ---------------- 注册 + 邮箱验证 ----------------

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
    body: RegisterRequest, request: Request, session: AsyncSession = Depends(get_db)
) -> RegisterResponse:
    """公开自助注册：建 `member` 角色用户 + 其私有 workspace（owner=本人），不签发 token。

    校验用户名非空+唯一、密码过策略（≥8 位），可选 `email`（填了则尽力发验证链接，SMTP 未配
    置静默跳过、不阻塞注册）。IP 级限速防批量注册。返回 201 + user/workspace，客户端随后走
    `/auth/login`。
    """
    ip = _client_ip(request)
    guard = _guard(request)
    if not guard.check_rate(ip):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")

    username = body.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名不能为空")
    if not reset_meets_policy(body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 8 位")
    email = body.email.strip()
    if email and ("@" not in email or "." not in email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "邮箱格式不正确")
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
        email=email,
        email_verified=False,
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

    if email:
        settings: Any = request.app.state.settings
        secret = _secret(request)
        if secret:
            token = jwt.encode(
                {"sub": str(user.id), "typ": "email_verify",
                 "iat": datetime.now(UTC),
                 "exp": datetime.now(UTC) + timedelta(seconds=EMAIL_VERIFY_TTL_SECONDS)},
                secret, algorithm="HS256",
            )
            send_verification_email(
                settings, to_email=email, username=username, token=token,
                base_url=getattr(settings, "web_base_url", ""),
            )

    return RegisterResponse(
        user=UserOut(
            id=user.id, username=user.username, display_name=user.display_name,
            email=user.email, email_verified=user.email_verified,
            enabled=user.enabled, role_id=member_role.id, role_name=member_role.name,
        ),
        workspace=WorkspaceOut(id=workspace.id, name=workspace.name, slug=workspace.slug),
    )


@router.get("/auth/verify-email")
async def verify_email(
    token: str, request: Request, session: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    """校验 email-verify JWT → 置 `email_verified=True`。过期/格式错 → 400。"""
    secret = _secret(request)
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证链接无效或已过期") from exc
    if payload.get("typ") != "email_verify":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证链接无效或已过期")
    uid = int(payload.get("sub") or 0)
    user = (await session.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "验证链接无效或已过期")
    user.email_verified = True
    await session.commit()
    return {"ok": True, "email_verified": True}

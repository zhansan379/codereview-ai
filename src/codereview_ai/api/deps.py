"""后台 REST 鉴权依赖（DESIGN §14.3 + F5.11 RBAC）。

`get_current_user` 解析 Bearer JWT（HS256，`secret_key` 来自 Settings），然后**按请求查库**
加载 `User` 行（连带角色与权限），user 不存在或已禁用 → 401（即时生效）。原 sync 版只返回
`sub` 字符串；改 async 后 router 级 `dependencies=[Depends(get_current_user)]` 不变
（FastAPI 自动 await）。webhook 路由不经过这里——webhook 靠签名鉴权。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, NoReturn

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette import status

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import Project, ProjectMember, ReviewTask, Role, User
from codereview_ai.storage.seed import PERMISSION_CATALOG

_bearer = HTTPBearer(auto_error=False)

#: 全局作用域权限码（role 直接授予、无需项目上下文即生效）
GLOBAL_PERM_CODES: frozenset[str] = frozenset(
    c for c, _name, scope, _desc in PERMISSION_CATALOG if scope == "global"
)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """每请求一个会话；引擎来自 `app.state.engine`（生命周期已建）。"""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "数据库未就绪")
    async with session_factory(engine)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def _reject() -> NoReturn:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或账号不可用")


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    """解析并校验 JWT，返回数据库里的 `User` 行（连带 role.permissions 已 eager 加载）。

    每次请求都查库：禁用用户即时拒绝、角色/权限改动即时生效。
    """
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        _reject()
    token = credentials.credentials
    settings = getattr(request.app.state, "settings", None)
    secret = getattr(settings, "secret_key", "") or ""
    if not secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "服务端密钥未配置")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 无效或已过期") from exc
    sub = payload.get("sub")
    if sub is None:
        _reject()
    try:
        uid = int(sub)
    except (TypeError, ValueError):
        _reject()
    user = (await session.execute(
        select(User)
        .where(User.id == uid)
        .options(selectinload(User.role).selectinload(Role.permissions))
    )).scalar_one_or_none()
    if user is None or not user.enabled:
        _reject()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def resolved_permission_codes(user: User) -> set[str]:
    """用户可用的权限码集合：超管恒为全量目录，否则取其角色权限集。"""
    if user.role.is_super:
        return {c for c, _name, _scope, _desc in PERMISSION_CATALOG}
    return {p.code for p in user.role.permissions}


async def user_can(
    session: AsyncSession, user: User, code: str, project_id: int | None = None
) -> bool:
    """判断用户对某个权限码（可选指定项目）是否有权限。

    - 超管：任何权限、任何项目均通过。
    - 权限码不在角色权限集：拒绝。
    - 全局权限：通过。
    - 项目权限：需 `role.all_projects`，或该用户是 `project_id` 的成员。
    """
    if user.role.is_super:
        return True
    if code not in resolved_permission_codes(user):
        return False
    if code in GLOBAL_PERM_CODES:
        return True
    # 项目级权限：全项目角色直通；否则需指定项目且是成员
    if user.role.all_projects:
        return True
    if project_id is None:
        return False
    return bool((await session.execute(
        select(ProjectMember.id).where(
            ProjectMember.user_id == user.id, ProjectMember.project_id == project_id
        )
    )).first())


async def allowed_project_ids(session: AsyncSession, user: User) -> tuple[bool, set[int]]:
    """返回 `(is_global, allowed_ids)`：超管/全项目角色 is_global=True（不过滤）；
    否则 `allowed_ids` 为该用户有成员关系的项目 id 集合。"""
    if user.role.is_super or user.role.all_projects:
        return True, set()
    rows = (await session.execute(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    )).scalars().all()
    return False, set(rows)


def review_scope_clause(is_global: bool, ids: set[int]) -> Any:
    """ReviewTask 的作用域谓词；is_global（超管/全项目角色）→ None（不过滤）。

    否则：`project_id in ids`，或 `project_id IS NULL`（存量历史行）但
    provider+repo_id 关联到某个成员项目（读侧兜底旧数据）。SQLite/Postgres 均兼容。
    """
    if is_global:
        return None
    # 空成员集时两分支自然均为 False（in_([]) 恒假），无需特判
    member_project = exists(select(Project.id).where(
        Project.provider == ReviewTask.provider,
        Project.repo_id == ReviewTask.repo_id,
        Project.id.in_(list(ids)),
    ))
    return or_(
        ReviewTask.project_id.in_(list(ids)),
        and_(ReviewTask.project_id.is_(None), member_project),
    )


async def review_task_project_id(session: AsyncSession, task: ReviewTask) -> int | None:
    """任务归属项目 id：有 `project_id` 直接用；存量 NULL 按 (provider, repo_id) 兜底解析。"""
    if task.project_id is not None:
        return task.project_id
    return (await session.execute(
        select(Project.id).where(
            Project.provider == task.provider, Project.repo_id == task.repo_id
        )
    )).scalar_one_or_none()


async def review_task_allowed(session: AsyncSession, user: User, task: ReviewTask) -> bool:
    """成员可见性：超管/全项目角色直接放行；否则任务归属项目须在成员 id 集合内。"""
    is_global, ids = await allowed_project_ids(session, user)
    if is_global:
        return True
    pid = await review_task_project_id(session, task)
    return pid is not None and pid in ids


def require_permission(*codes: str) -> Any:
    """依赖工厂：拥有任意一个所给权限（全局或全项目角色即可）→ 放行，否则 403。

    router 级全局门禁（如 `schedules:manage`）与新增 users/roles 路由用。
    """

    async def _dep(user: CurrentUser, session: AsyncSession = Depends(get_db)) -> User:
        for code in codes:
            if await user_can(session, user, code):
                return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限")

    return _dep


#: 便于 import：require_any_permission == require_permission（工厂本就接受多码）
require_any_permission = require_permission

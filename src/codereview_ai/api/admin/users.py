"""用户管理 REST（F5.11）：增删改用户、启停、重置密码。`users:manage`（管理员）。

禁用/删除最后一位启用超管会锁死系统 → 服务端强校验：不可删自己、不可删/禁而把
启用超管数清零。密码创建与重置统一走 `security` 策略（最小长度）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette import status

from codereview_ai.api.deps import CurrentUser, get_current_user, get_db, require_permission
from codereview_ai.security import hash_password, reset_meets_policy
from codereview_ai.storage.models import Role, User

router = APIRouter(
    prefix="/users",
    dependencies=[Depends(get_current_user), Depends(require_permission("users:manage"))],
)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str = ""
    enabled: bool
    role_id: int
    role_name: str = ""
    created_at: datetime


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role_id: int
    enabled: bool = True


class UserUpdate(BaseModel):
    display_name: str | None = None
    role_id: int | None = None
    enabled: bool | None = None


class PasswordWrite(BaseModel):
    password: str


async def _get_or_404(session: AsyncSession, user_id: int) -> User:
    row = (await session.execute(
        select(User).where(User.id == user_id).options(selectinload(User.role))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    return row


def _to_out(row: User) -> UserOut:
    return UserOut(
        id=row.id, username=row.username, display_name=row.display_name,
        enabled=row.enabled, role_id=row.role_id,
        role_name=row.role.name if row.role else "", created_at=row.created_at,
    )


async def _enabled_super_count(session: AsyncSession) -> int:
    return int((await session.execute(
        select(func.count()).select_from(User)
        .join(Role, Role.id == User.role_id)
        .where(User.enabled.is_(True), Role.is_super.is_(True))
    )).scalar_one() or 0)


@router.get("", response_model=list[UserOut])
async def list_users(session: AsyncSession = Depends(get_db)) -> list[UserOut]:
    rows = (await session.execute(
        select(User).order_by(User.id).options(selectinload(User.role))
    )).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, session: AsyncSession = Depends(get_db)) -> UserOut:
    username = body.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名不能为空")
    if not reset_meets_policy(body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 8 位")
    role = (await session.execute(select(Role).where(Role.id == body.role_id))).scalar_one_or_none()
    if role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "角色不存在")
    row = User(username=username, password_hash=hash_password(body.password),
               display_name=body.display_name, enabled=body.enabled, role_id=role.id)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from None
    await session.refresh(row)
    row.role = role
    return _to_out(row)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, body: UserUpdate, session: AsyncSession = Depends(get_db)
) -> UserOut:
    row = await _get_or_404(session, user_id)
    # 禁停最后一个启用超管 → 400（防止锁死系统，含自身）；改超管角色/降权同理不在此拦
    if body.enabled is False and row.role.is_super:
        if await _enabled_super_count(session) <= 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能禁用最后一个管理员")
    if body.display_name is not None:
        row.display_name = body.display_name
    if body.role_id is not None:
        new_role = (await session.execute(
            select(Role).where(Role.id == body.role_id))).scalar_one_or_none()
        if new_role is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "角色不存在")
        row.role_id = new_role.id
        row.role = new_role
    if body.enabled is not None:
        row.enabled = body.enabled
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int, user: CurrentUser, session: AsyncSession = Depends(get_db)
) -> None:
    row = await _get_or_404(session, user_id)
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除当前登录用户")
    if row.role.is_super and await _enabled_super_count(session) <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除最后一个管理员")
    await session.delete(row)
    await session.commit()


@router.post("/{user_id}/reset-password", response_model=UserOut)
async def reset_password(
    user_id: int, body: PasswordWrite, session: AsyncSession = Depends(get_db)
) -> UserOut:
    row = await _get_or_404(session, user_id)
    if not reset_meets_policy(body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 8 位")
    row.password_hash = hash_password(body.password)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)

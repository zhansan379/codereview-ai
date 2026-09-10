"""项目管理 REST（DESIGN §14.2，F5.3 + F5.11）：注册项目 + 分支规则 + 审查策略等。

RBAC：读需 `projects:view`（按成员关系过滤），写需 `projects:manage`；成员端点
（`GET/PUT /projects/{id}/members`）同受 `projects:manage` 约束。超管/全项目角色全量可见。
读请求体经 pydantic 校验；JWT + DB 依赖注入。offline 可测：httpx ASGI + 临时 SQLite。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_project_ids,
    get_current_user,
    get_db,
    user_can,
)
from codereview_ai.storage.models import Project, ProjectMember, User

router = APIRouter(prefix="/projects", dependencies=[Depends(get_current_user)])


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    repo_id: str
    repo_full_name: str
    web_url: str
    branch_rule: str
    file_extensions: str
    review_strategy: str
    prompt_suffix: str
    score_threshold: int
    enforce_score_threshold: bool = False
    notifier_routing: dict[str, Any]
    enabled: bool
    push_enabled: bool | None = None
    push_branch_globs: str = ""
    mr_enabled: bool | None = None


class ProjectWrite(BaseModel):
    provider: str = ""
    repo_id: str = ""
    repo_full_name: str = ""
    web_url: str = ""
    branch_rule: str = ""
    file_extensions: str = ""
    review_strategy: str = "diff"
    prompt_suffix: str = ""
    score_threshold: int = 80
    enforce_score_threshold: bool = False
    notifier_routing: dict[str, Any] = {}
    enabled: bool = True
    push_enabled: bool | None = None
    push_branch_globs: str = ""
    # MR 审查（与 push 对称）：None=继承全局默认；True/False=显式覆盖
    mr_enabled: bool | None = None


class ProjectMemberOut(BaseModel):
    """成员列表项：用户简要信息。"""

    id: int
    username: str
    display_name: str = ""


class ProjectMembersWrite(BaseModel):
    user_ids: list[int] = []


async def _get_or_404(session: AsyncSession, project_id: int) -> Project:
    row = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    return row


def _forbid() -> None:
    raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限操作该项目")


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> list[Project]:
    stmt = select(Project)
    is_global, ids = await allowed_project_ids(session, user)
    if not is_global:
        stmt = stmt.where(Project.id.in_(list(ids)))
    rows = (await session.execute(stmt.order_by(Project.id))).scalars().all()
    return list(rows)


_DUPLICATE_MSG = "同平台下已存在该仓库 ID 的项目，请勿重复添加"


async def _duplicate_project(
    session: AsyncSession, *, provider: str, repo_id: str, exclude_id: int | None = None
) -> bool:
    """(provider, repo_id) 是否已被占用（update 时排除自身）。"""
    stmt = select(Project.id).where(
        Project.provider == provider, Project.repo_id == repo_id
    )
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectWrite,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> Project:
    if not await user_can(session, user, "projects:manage", project_id=None):
        _forbid()
    provider = body.provider.strip() if body.provider else ""
    repo_id = body.repo_id.strip() if body.repo_id else ""
    if not provider or not repo_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "平台与仓库 ID 均必填")
    if await _duplicate_project(session, provider=provider, repo_id=repo_id):
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_MSG)
    row = Project(**body.model_dump())
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_MSG) from None
    await session.refresh(row)
    # 创建者自动入成员表，保证非全项目角色的创建者能看到自己建的项目
    session.add(ProjectMember(project_id=row.id, user_id=user.id))
    await session.commit()
    return row


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: int,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> Project:
    row = await _get_or_404(session, project_id)
    if not await user_can(session, user, "projects:view", project_id=row.id):
        _forbid()
    return row


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectWrite,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> Project:
    row = await _get_or_404(session, project_id)
    if not await user_can(session, user, "projects:manage", project_id=row.id):
        _forbid()
    provider = body.provider.strip() if body.provider else row.provider
    repo_id = body.repo_id.strip() if body.repo_id else row.repo_id
    if await _duplicate_project(
        session, provider=provider, repo_id=repo_id, exclude_id=project_id
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_MSG)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return row


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> None:
    row = await _get_or_404(session, project_id)
    if not await user_can(session, user, "projects:manage", project_id=row.id):
        _forbid()
    # 成员关系随项目级联删除（project_member.project_id ondelete=CASCADE）
    await session.delete(row)
    await session.commit()


# ── 成员管理（F5.11 项目级隔离；projects:manage 作用域）──────────────

@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: int,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> list[ProjectMemberOut]:
    row = await _get_or_404(session, project_id)
    if not await user_can(session, user, "projects:manage", project_id=row.id):
        _forbid()
    pairs = (await session.execute(
        select(User, ProjectMember)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.id)
    )).all()
    return [ProjectMemberOut(id=u.id, username=u.username, display_name=u.display_name)
            for u, _pm in pairs]


@router.put("/{project_id}/members", response_model=list[ProjectMemberOut])
async def set_project_members(
    project_id: int,
    body: ProjectMembersWrite,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> list[ProjectMemberOut]:
    row = await _get_or_404(session, project_id)
    if not await user_can(session, user, "projects:manage", project_id=row.id):
        _forbid()
    # 全量替换：清空该项目成员，再按 user_ids 重建
    await session.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id))
    if body.user_ids:
        existing = set((await session.execute(
            select(User.id).where(User.id.in_(body.user_ids))
        )).scalars().all())
        for uid in dict.fromkeys(body.user_ids):  # 去重保序
            if uid in existing:
                session.add(ProjectMember(project_id=project_id, user_id=uid))
    await session.commit()
    return await list_project_members(project_id, session=session, user=user)

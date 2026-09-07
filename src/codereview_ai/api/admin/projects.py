"""项目管理 REST（DESIGN §14.2，F5.3）：注册项目 + 分支规则 + 审查策略等。

读请求体经 pydantic 校验；JWT + DB 依赖注入。offline 可测：httpx ASGI + 临时 SQLite。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.storage.models import Project

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
    # push 审查（DESIGN §7.7）：None=继承全局 env 默认；True/False=显式覆盖；glob 非空则覆盖全局分支规则
    push_enabled: bool | None = None
    push_branch_globs: str = ""


async def _get_or_404(session: AsyncSession, project_id: int) -> Project:
    row = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    return row


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: AsyncSession = Depends(get_db)) -> list[Project]:
    rows = (await session.execute(select(Project).order_by(Project.id))).scalars().all()
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
async def create_project(body: ProjectWrite, session: AsyncSession = Depends(get_db)) -> Project:
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
        # 并发下仍可能撞 (provider, repo_id) 唯一键 → 统一转 409
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_MSG)
    await session.refresh(row)
    return row


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, session: AsyncSession = Depends(get_db)) -> Project:
    return await _get_or_404(session, project_id)


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int, body: ProjectWrite, session: AsyncSession = Depends(get_db)
) -> Project:
    row = await _get_or_404(session, project_id)
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
async def delete_project(project_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_or_404(session, project_id)
    await session.delete(row)
    await session.commit()

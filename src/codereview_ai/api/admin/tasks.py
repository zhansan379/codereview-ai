"""任务监控 REST（DESIGN §14.2）：任务列表 + 手动重试。

`POST /tasks/{id}/retry` 把 failed 任务重置为 queued 并 attempt+1（DESIGN §9.2），
便于后台一键重新入队。重试仅对失败态生效，其余状态 409。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.storage.models import ReviewTask

router = APIRouter(prefix="/tasks", dependencies=[Depends(get_current_user)])


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    repo_id: str
    pr_number: int | None
    event_type: str
    branch: str
    head_sha: str
    state: str
    attempt: int
    error: str
    queued_at: datetime


class TaskRetried(BaseModel):
    id: int
    state: str
    attempt: int


async def _get_or_404(session: AsyncSession, task_id: int) -> ReviewTask:
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == task_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return row


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    session: AsyncSession = Depends(get_db), state: str | None = None
) -> list[TaskOut]:
    stmt = select(ReviewTask)
    if state:
        stmt = stmt.where(ReviewTask.state == state)
    rows = (await session.execute(stmt.order_by(ReviewTask.id.desc()))).scalars().all()
    return [TaskOut.model_validate(r) for r in rows]


@router.post("/{task_id}/retry", response_model=TaskRetried)
async def retry_task(task_id: int, session: AsyncSession = Depends(get_db)) -> TaskRetried:
    row = await _get_or_404(session, task_id)
    if row.state != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅 failed 状态可重试（当前 {row.state}）")
    row.state = "queued"
    row.attempt += 1
    row.error = ""
    await session.commit()
    await session.refresh(row)
    return TaskRetried(id=row.id, state=row.state, attempt=row.attempt)

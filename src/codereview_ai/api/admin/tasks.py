"""任务监控 REST（DESIGN §14.2）：任务列表 + 手动重试。

`POST /tasks/{id}/retry` 把 failed 任务重置为 queued 并 attempt+1（DESIGN §9.2），
便于后台一键重新入队。重试仅对失败态生效，其余状态 409。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.storage.models import ReviewTask, _utcnow

logger = logging.getLogger("codereview_ai.api.tasks")

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
async def retry_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskRetried:
    row = await _get_or_404(session, task_id)
    if row.state != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅 failed 状态可重试（当前 {row.state}）")
    row.state = "queued"
    row.attempt += 1
    row.error = ""
    row.queued_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    # simple 档队列：仅翻 DB 侧 queued 不会让内存 worker 重新拾取。持原事件且
    # enqueuer 就绪时把任务重新投进队列，worker 才会真去跑；否则退化为只记状态翻转。
    enqueuer = getattr(request.app.state, "enqueuer", None)
    if enqueuer is not None and row.payload:
        await enqueuer.enqueue(row.provider, row.payload.encode())
        logger.info("重试任务 %s：已重新入队（provider=%s）", row.id, row.provider)
    return TaskRetried(id=row.id, state=row.state, attempt=row.attempt)

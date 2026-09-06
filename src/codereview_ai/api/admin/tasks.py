"""任务监控 REST（DESIGN §14.2）：任务列表 + 手动重试。

`POST /tasks/{id}/retry` 把 failed（或可补审的 push skipped）重置为 queued 并 attempt+1
（DESIGN §9.2），便于后台一键重新入队/补审。failed 与门控/配置类 skipped 可重试，
删分支（branch_deleted）等其余状态 409。push 轨重试会置 force_rerun 以绕过幂等预检。
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
    skip_reason: str
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


#: 只要「门控/配置类」跳过的可补审；删分支（branch_deleted）无 head 可审，禁止
_RETRYABLE_SKIPPED_REASONS = {"push_disabled", "branch_mismatch"}


def _retryable(row: ReviewTask) -> bool:
    """该行是否允许手动重试/补审。

    - `failed` → 照旧可重试；
    - `skipped` + push 轨 + 门控/配置类原因 → 可补审（前端「补审」，绕过门控强审）；
    - 其余（queued/running/completed、branch_deleted、mr 轨 skipped）→ 不可。
    """
    if row.state == "failed":
        return True
    return bool(
        row.state == "skipped"
        and row.event_type == "push"
        and row.skip_reason in _RETRYABLE_SKIPPED_REASONS
    )


@router.post("/{task_id}/retry", response_model=TaskRetried)
async def retry_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskRetried:
    row = await _get_or_404(session, task_id)
    if not _retryable(row):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"该任务当前状态不可重试（{row.state}）",
        )
    row.state = "queued"
    row.attempt += 1
    row.error = ""
    row.skip_reason = ""
    row.queued_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    # simple 档队列：仅翻 DB 侧 queued 不会让内存 worker 重新拾取。持原事件且
    # enqueuer 就绪时把任务重新投进队列，worker 才会真去跑；否则退化为只记状态翻转。
    enqueuer = getattr(request.app.state, "enqueuer", None)
    if enqueuer is not None and row.payload:
        # push 轨重跑会被幂等预检/门控短路；置 force_rerun 由 worker 强制补审（DESIGN §7.7）。
        # 仅 push 任务设，避免在 mr 行遗留无意义标记。
        if row.event_type == "push":
            row.force_rerun = True
            await session.commit()
        await enqueuer.enqueue(row.provider, row.payload.encode())
        logger.info("重试任务 %s：已重新入队（provider=%s）", row.id, row.provider)
    return TaskRetried(id=row.id, state=row.state, attempt=row.attempt)

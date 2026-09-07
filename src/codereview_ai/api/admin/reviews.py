"""审查记录 REST（DESIGN §14.2）：列表 + 详情（含 findings 汇总）。

服务端分页（limit/offset），禁止把全表 SELECT 进前端（DESIGN §14.1）；可选按 state 过滤。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.storage.models import ReviewFinding, ReviewTask

router = APIRouter(prefix="/reviews", dependencies=[Depends(get_current_user)])


class ReviewListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    repo_id: str
    pr_number: int | None
    pr_title: str
    event_type: str
    branch: str
    head_sha: str
    base_sha: str
    state: str
    attempt: int
    error: str
    skip_reason: str
    trace_id: str
    summary_md: str
    score_total: int
    queued_at: datetime
    finished_at: datetime | None


class ReviewDetail(ReviewListItem):
    findings: list[ReviewFindingOut] = []


class FindingStatusUpdate(BaseModel):
    status: str


class ReviewFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    severity: str
    category: str
    file: str
    old_line: int | None
    new_line: int | None
    title: str
    detail: str
    existing_code: str
    suggestion: str
    source: str
    status: str
    first_seen: datetime
    last_seen: datetime
    reopened_count: int


class ReviewPage(BaseModel):
    items: list[ReviewListItem]
    total: int
    limit: int
    offset: int


@router.get("", response_model=ReviewPage)
async def list_reviews(
    session: AsyncSession = Depends(get_db),
    state: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ReviewPage:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = select(ReviewTask)
    count_stmt = select(func.count()).select_from(ReviewTask)
    if state:
        stmt = stmt.where(ReviewTask.state == state)
        count_stmt = count_stmt.where(ReviewTask.state == state)
    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(
        stmt.order_by(ReviewTask.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    items = [ReviewListItem.model_validate(r) for r in rows]
    return ReviewPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(review_id: int, session: AsyncSession = Depends(get_db)) -> ReviewDetail:
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == review_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审查记录不存在")
    findings = (await session.execute(
        select(ReviewFinding).where(ReviewFinding.task_id == review_id).order_by(
            ReviewFinding.id
        )
    )).scalars().all()
    detail = ReviewDetail(**ReviewListItem.model_validate(row).model_dump())
    detail.findings = [ReviewFindingOut.model_validate(f) for f in findings]
    return detail


@router.post("/findings/{finding_id}/status", response_model=ReviewFindingOut)
async def set_finding_status(
    finding_id: int,
    body: FindingStatusUpdate,
    session: AsyncSession = Depends(get_db),
) -> ReviewFinding:
    """人工更新 finding 状态（DESIGN §7.3）：`waived`（搁置/忽略）↔ `active`（恢复）。

    `resolved` 由 worker 非增量全量对账自动托管，禁止手改（防误判已解决埋 bug）。
    """
    if body.status not in {"waived", "active"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "仅支持 waived/active；resolved 由系统对账托管，禁止手改",
        )
    from codereview_ai.storage.models import _utcnow

    row = (await session.execute(
        select(ReviewFinding).where(ReviewFinding.id == finding_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding 不存在")
    row.status = body.status
    row.last_seen = _utcnow()
    await session.commit()
    await session.refresh(row)
    return row

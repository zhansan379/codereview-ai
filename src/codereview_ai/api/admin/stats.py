"""看板统计服务端聚合（DESIGN §14.1）：`GET /api/stats`（JWT 鉴权）。

—— 禁止全表 SELECT 进前端 ——
所有指标都在 DB 侧用 `func.count / GROUP BY` 聚合后再返回，只回传统计值。
覆盖：任务状态分布、finding 严重级/类别分布、近 14 天每日审查量、token 用量、
provider 分流、汇总计数。`reviews_by_day` 服务端补零，前端直接画图不用自补。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_project_ids,
    get_current_user,
    get_db,
    review_scope_clause,
)
from codereview_ai.storage.models import ModelUsage, ReviewFinding, ReviewTask

router = APIRouter(prefix="/stats", dependencies=[Depends(get_current_user)])


class CountItem(BaseModel):
    key: str
    count: int


class ReviewsByDay(BaseModel):
    day: str  # YYYY-MM-DD
    count: int


class ModelUsageItem(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class StatsOut(BaseModel):
    total_tasks: int
    total_findings: int
    open_critical: int
    open_high: int
    tasks_by_state: list[CountItem]
    findings_by_severity: list[CountItem]
    findings_by_category: list[CountItem]
    reviews_by_day: list[ReviewsByDay]
    model_usage: list[ModelUsageItem]
    provider_split: list[CountItem]


async def _counts(session: AsyncSession, col: Any, where: Any = None) -> list[CountItem]:
    """`GROUP BY col` → 归一化的 (key, count) 列表，计数为 0 的组不出现；`where` 可选。"""
    stmt = select(col, func.count().label("n")).group_by(col)
    if where is not None:
        stmt = stmt.where(where)
    rows = (await session.execute(stmt)).all()
    return [CountItem(key=str(row[0] or "未知"), count=int(row[1])) for row in rows]


async def aggregate_stats(session: AsyncSession, task_filter: Any | None = None) -> StatsOut:
    """在给定会话上聚合全部看板指标（路由与日报共用，M5.7 复用）。

    `task_filter` 为可作用于 `ReviewTask` 的 SQLAlchemy 谓词（RBAC 项目隔离用）；
    None = 全量（日报/超管）。findings/usage 经 task_id 关联后同样被过滤。
    """
    _f = task_filter
    total_tasks = int((await session.execute(
        select(func.count()).select_from(ReviewTask).where(_f) if _f is not None else
        select(func.count()).select_from(ReviewTask)
    )).scalar_one() or 0)

    findings_from = select(func.count()).select_from(ReviewFinding).join(
        ReviewTask, ReviewTask.id == ReviewFinding.task_id)
    total_findings = int((await session.execute(
        findings_from.where(_f) if _f is not None else findings_from
    )).scalar_one() or 0)

    def _finding_where(severity: str) -> Any:
        w = [ReviewFinding.severity == severity, ReviewFinding.status == "active"]
        return (and_(_f, *w) if _f is not None else and_(*w))

    open_critical = int((await session.execute(
        findings_from.where(_finding_where("critical")))).scalar_one() or 0)
    open_high = int((await session.execute(
        findings_from.where(_finding_where("high")))).scalar_one() or 0)

    # findings 无 project 归属，按 task_id 子查询过滤（_counts 无 join 能力）
    _finding_scope = (None if _f is None else
                      ReviewFinding.task_id.in_(select(ReviewTask.id).where(_f)))
    tasks_by_state = await _counts(session, ReviewTask.state, where=_f)
    findings_by_severity = await _counts(session, ReviewFinding.severity, where=_finding_scope)
    findings_by_category = await _counts(session, ReviewFinding.category, where=_finding_scope)
    provider_split = await _counts(session, ReviewTask.provider, where=_f)

    # 近 14 天每日审查量（服务端补零，缺失日期填 0）
    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=13)
    day_stmt = (select(func.date(ReviewTask.queued_at), func.count().label("n"))
                .where(ReviewTask.queued_at >= start_day).group_by(func.date(ReviewTask.queued_at)))
    if _f is not None:
        day_stmt = day_stmt.where(_f)
    by_day_rows = (await session.execute(day_stmt)).all()
    by_day = {str(r[0]): int(r[1]) for r in by_day_rows if r[0] is not None}
    reviews_by_day = [
        ReviewsByDay(day=str(start_day + timedelta(days=i)),
                     count=by_day.get(str(start_day + timedelta(days=i)), 0))
        for i in range(14)
    ]

    usage_stmt = (select(
            ModelUsage.model,
            func.count().label("reqs"),
            func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
            func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        ).join(ReviewTask, ReviewTask.id == ModelUsage.task_id).group_by(ModelUsage.model))
    if _f is not None:
        usage_stmt = usage_stmt.where(_f)
    usage_rows = (await session.execute(usage_stmt)).all()
    model_usage = [
        ModelUsageItem(model=str(r[0] or "未知"), requests=int(r[1]),
                       prompt_tokens=int(r[2]), completion_tokens=int(r[3]),
                       total_tokens=int(r[4]))
        for r in usage_rows
    ]

    return StatsOut(
        total_tasks=total_tasks, total_findings=total_findings,
        open_critical=open_critical, open_high=open_high,
        tasks_by_state=tasks_by_state, findings_by_severity=findings_by_severity,
        findings_by_category=findings_by_category, reviews_by_day=reviews_by_day,
        model_usage=model_usage, provider_split=provider_split,
    )


@router.get("", response_model=StatsOut)
async def get_stats(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> StatsOut:
    """聚合全部看板指标，一次调用出全量（前端只发一次请求）；按成员关系隔离。"""
    is_global, ids = await allowed_project_ids(session, user)
    task_filter = review_scope_clause(is_global, ids)
    return await aggregate_stats(session, task_filter=task_filter)

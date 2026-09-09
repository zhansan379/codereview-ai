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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import get_current_user, get_db
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


class AgentScatterItem(BaseModel):
    """复杂度×成本散点单点（一条 agent 审查）。

    diff_lines=新增+删除合计；duration_s=started→finished 纯 agent 运行秒；tool_calls、
    chat_rounds 反映探索轮数。论证「越复杂 → 工具轮数越多 → 响应越久」。
    """
    diff_lines: int
    duration_s: int
    chat_rounds: int
    tool_calls: int


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
    tasks_by_mode: list[CountItem]  # exec_mode 分布（agent / diff）
    agent_task_count: int  # agent 实际执行的任务数
    avg_chat_rounds: float  # agent 平均对话轮数
    agent_scatter: list[AgentScatterItem]


async def _counts(session: AsyncSession, col: Any) -> list[CountItem]:
    """`GROUP BY col` → 归一化的 (key, count) 列表，计数为 0 的组不出现。"""
    rows = (await session.execute(
        select(col, func.count().label("n")).group_by(col)
    )).all()
    return [CountItem(key=str(row[0] or "未知"), count=int(row[1])) for row in rows]


async def aggregate_stats(session: AsyncSession) -> StatsOut:
    """在给定会话上聚合全部看板指标（路由与日报共用，M5.7 复用）。"""
    total_tasks = int((await session.execute(
        select(func.count()).select_from(ReviewTask)
    )).scalar_one() or 0)
    total_findings = int((await session.execute(
        select(func.count()).select_from(ReviewFinding)
    )).scalar_one() or 0)

    open_critical = int((await session.execute(
        select(func.count()).select_from(ReviewFinding).where(
            ReviewFinding.severity == "critical", ReviewFinding.status == "active"
        )
    )).scalar_one() or 0)
    open_high = int((await session.execute(
        select(func.count()).select_from(ReviewFinding).where(
            ReviewFinding.severity == "high", ReviewFinding.status == "active"
        )
    )).scalar_one() or 0)

    tasks_by_state = await _counts(session, ReviewTask.state)
    findings_by_severity = await _counts(session, ReviewFinding.severity)
    findings_by_category = await _counts(session, ReviewFinding.category)
    provider_split = await _counts(session, ReviewTask.provider)
    tasks_by_mode = await _counts(session, ReviewTask.exec_mode)

    # agent 实际执行任务数 + 平均对话轮数（KPI 用；exec_mode 由 worker 落真实路径）
    agent_row = (await session.execute(
        select(
            func.count(),
            func.coalesce(func.avg(ReviewTask.chat_rounds), 0),
        ).where(ReviewTask.exec_mode == "agentic")
    )).one()
    agent_task_count = int(agent_row[0] or 0)
    avg_chat_rounds = round(float(agent_row[1] or 0), 2)

    # 复杂度×成本散点：最近 200 条完成、时间戳齐全的 agent 审查
    #（duration_s 取 started→finished 纯运行时长，不含排队；仅 agent 模式，diff 无工具轮数）。
    scatter_rows = (await session.execute(
        select(
            ReviewTask.diff_lines, ReviewTask.started_at, ReviewTask.finished_at,
            ReviewTask.chat_rounds, ReviewTask.tool_calls,
        )
        .where(
            ReviewTask.exec_mode == "agentic",
            ReviewTask.state == "completed",
            ReviewTask.started_at.isnot(None),
            ReviewTask.finished_at.isnot(None),
        )
        .order_by(ReviewTask.id.desc())
        .limit(200)
    )).all()
    agent_scatter = [
        AgentScatterItem(
            diff_lines=int(r[0] or 0),
            duration_s=int((r[2] - r[1]).total_seconds()),
            chat_rounds=int(r[3] or 0),
            tool_calls=int(r[4] or 0),
        )
        for r in scatter_rows
        if r[1] is not None and r[2] is not None and (r[2] - r[1]).total_seconds() >= 0
    ]

    # 近 14 天每日审查量（服务端补零，缺失日期填 0）
    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=13)
    by_day_rows = (await session.execute(
        select(func.date(ReviewTask.queued_at), func.count().label("n"))
        .where(ReviewTask.queued_at >= start_day)
        .group_by(func.date(ReviewTask.queued_at))
    )).all()
    by_day = {str(r[0]): int(r[1]) for r in by_day_rows if r[0] is not None}
    reviews_by_day = [
        ReviewsByDay(day=str(start_day + timedelta(days=i)),
                     count=by_day.get(str(start_day + timedelta(days=i)), 0))
        for i in range(14)
    ]

    usage_rows = (await session.execute(
        select(
            ModelUsage.model,
            func.count().label("reqs"),
            func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
            func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        ).group_by(ModelUsage.model)
    )).all()
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
        tasks_by_mode=tasks_by_mode, agent_task_count=agent_task_count,
        avg_chat_rounds=avg_chat_rounds, agent_scatter=agent_scatter,
    )


@router.get("", response_model=StatsOut)
async def get_stats(session: AsyncSession = Depends(get_db)) -> StatsOut:
    """聚合全部看板指标，一次调用出全量（前端只发一次请求）。"""
    return await aggregate_stats(session)

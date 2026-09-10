"""看板统计服务端聚合（DESIGN §14.1）：`GET /api/stats`（JWT 鉴权）。

—— 禁止全表 SELECT 进前端 ——
所有指标都在 DB 侧用 `func.count / GROUP BY` 聚合后再返回，只回传统计值。
覆盖：任务状态分布、finding 严重级/类别分布、近 14 天每日审查量、token 用量、
provider 分流、汇总计数。`reviews_by_day` 服务端补零，前端直接画图不用自补。
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_project_ids,
    get_current_user,
    get_db,
    review_scope_clause,
)
from codereview_ai.storage.models import (
    ModelUsage,
    ReviewConversation,
    ReviewFinding,
    ReviewTask,
)

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
    cost: float  # 该模型累计成本（ModelUsage.cost 求和）


class DailyCostItem(BaseModel):
    """近 14 天每日成本（服务端补零，缺成本日填 0.0）。"""
    day: str  # YYYY-MM-DD
    cost: float


class DailyDurationItem(BaseModel):
    """近 14 天单任务平均耗时（按完成日分桶；无任务日 avg_seconds=0 且 count=0）。"""
    day: str  # YYYY-MM-DD
    count: int  # 该日完成、时间戳齐全的任务数
    avg_seconds: float  # started→finished 平均运行秒（不含排队）


class AgentScatterItem(BaseModel):
    """复杂度×成本散点单点（一条 agent 审查）。

    diff_lines=新增+删除合计；duration_s=started→finished 纯 agent 运行秒；tool_calls、
    chat_rounds 反映探索轮数。论证「越复杂 → 工具轮数越多 → 响应越久」。
    """
    diff_lines: int
    duration_s: int
    chat_rounds: int
    tool_calls: int


class PhaseBoxItem(BaseModel):
    """Agent 某阶段单次审查（task）调用 LLM 次数的分布统计（箱线图一个箱体）。

    每条对应一个 phase；`task_count` 是该阶段有调用的 task 数；min/q1/median/q3/max
    供画箱须；mean 平均值、mode 众数（并列取最小）供散点标注。
    """
    key: str
    task_count: int
    min: int
    q1: float
    median: float
    q3: float
    max: int
    mean: float
    mode: int


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
    cost_by_day: list[DailyCostItem]  # 近 14 天每日成本趋势
    duration_by_day: list[DailyDurationItem]  # 近 14 天单任务平均耗时
    phase_box: list[PhaseBoxItem]  # agent 各阶段单次审查调用次数分布（箱线图）
    provider_split: list[CountItem]
    tasks_by_mode: list[CountItem]  # exec_mode 分布（agent / diff）
    agent_task_count: int  # agent 实际执行的任务数
    avg_chat_rounds: float  # agent 平均对话轮数
    agent_scatter: list[AgentScatterItem]


async def _counts(session: AsyncSession, col: Any, *wheres: Any) -> list[CountItem]:
    """`GROUP BY col` → 归一化的 (key, count) 列表，计数为 0 的组不出现。

    `wheres` 为可选过滤条件（None 项忽略），供 mode 分组复用。
    """
    stmt = select(col, func.count().label("n"))
    for w in wheres:
        if w is not None:
            stmt = stmt.where(w)
    rows = (await session.execute(stmt.group_by(col))).all()
    return [CountItem(key=str(row[0] or "未知"), count=int(row[1])) for row in rows]


def _mode_clause(mode: str) -> tuple[Any | None, Any | None]:
    """返回（ReviewTask.exec_mode 过滤, ReviewTask.id 子查询）以按模式分组通用图。

    `mode='all'` 时两者皆 None（不追加过滤，保持全量）；`agentic`/`diff` 时分别覆盖
    ReviewTask 上直接可过滤的聚合，以及需经 task 关联的 ReviewFinding / ModelUsage。
    """
    if mode not in ("agentic", "diff"):
        return None, None
    clause = ReviewTask.exec_mode == mode
    return clause, select(ReviewTask.id).where(clause)


def _mode(vals: list[int]) -> int:
    """众数：出现频率最高者；并列取最小值（确定性）。"""
    freq = Counter(vals)
    top = max(freq.values())
    return min(v for v, n in freq.items() if n == top)


async def phase_box_stats(
    session: AsyncSession,
    task_filter: Any | None = None,
) -> list[PhaseBoxItem]:
    """Agent 各阶段单次审查调用次数分布（diff 无对话，天然只含 agent）。

    按 `(task_id, phase)` 分组数 conversation 行数固着（一条 conversation = 一次
    `llm.chat()`），再在每个 phase 内聚合 min/q1/median/q3/max/mean/mode。
    `task_filter`（RBAC 项目隔离）经 task 关联过滤，None = 全量。
    """
    box = select(ReviewConversation.task_id, ReviewConversation.phase, func.count())
    if task_filter is not None:
        box = box.join(ReviewTask, ReviewTask.id == ReviewConversation.task_id).where(task_filter)
    rows = (await session.execute(
        box.group_by(ReviewConversation.task_id, ReviewConversation.phase)
    )).all()
    by_phase: dict[str, list[int]] = {}
    for _tid, phase, n in rows:
        by_phase.setdefault(str(phase or "未知"), []).append(int(n))

    out: list[PhaseBoxItem] = []
    for key, vals in by_phase.items():
        if len(vals) >= 2:
            q1, median, q3 = statistics.quantiles(vals, n=4)
        else:
            q1 = median = q3 = float(vals[0])  # 单值：箱体收成一条线
        out.append(PhaseBoxItem(
            key=key,
            task_count=len(vals),
            min=min(vals),
            q1=q1,
            median=median,
            q3=q3,
            max=max(vals),
            mean=round(sum(vals) / len(vals), 2),
            mode=_mode(vals),
        ))
    return out


async def aggregate_stats(
    session: AsyncSession,
    mode: str = "all",
    task_filter: Any | None = None,
) -> StatsOut:
    """聚合全部看板指标；`mode` 按 exec_mode 过滤通用图（all/agentic/diff）。

    `task_filter`（RBAC 项目隔离）为可作用于 ReviewTask 的谓词；None = 全量。
    agent 专属度量（散点/平均轮数/阶段管线/模式饼）不受 `mode` 影响，但受 `task_filter` 过滤。
    """
    task_clause, mode_subq = _mode_clause(mode)

    # RBAC 项目隔离：task 直接过滤；findings/usage 经 task_id 子查询过滤
    task_subq = (select(ReviewTask.id).where(task_filter) if task_filter is not None else None)

    def finding_where(*cond: Any) -> tuple[Any, ...]:
        out = list(cond)
        if mode_subq is not None:
            out.append(ReviewFinding.task_id.in_(mode_subq))
        if task_subq is not None:
            out.append(ReviewFinding.task_id.in_(task_subq))
        return tuple(out)

    def task_filters() -> list[Any]:
        return [c for c in (task_clause, task_filter) if c is not None]

    def rbac_filters() -> list[Any]:
        # agent 专属度量只受 RBAC 项目隔离，不受 mode 影响
        return [task_filter] if task_filter is not None else []

    base_task = select(func.count()).select_from(ReviewTask)
    for w in task_filters():
        base_task = base_task.where(w)
    total_tasks = int((await session.execute(base_task)).scalar_one() or 0)

    total_findings = int((await session.execute(
        select(func.count()).select_from(ReviewFinding).where(*finding_where())
    )).scalar_one() or 0)

    open_critical = int((await session.execute(
        select(func.count()).select_from(ReviewFinding).where(
            *finding_where(ReviewFinding.severity == "critical", ReviewFinding.status == "active")
        )
    )).scalar_one() or 0)
    open_high = int((await session.execute(
        select(func.count()).select_from(ReviewFinding).where(
            *finding_where(ReviewFinding.severity == "high", ReviewFinding.status == "active")
        )
    )).scalar_one() or 0)

    tasks_by_state = await _counts(session, ReviewTask.state, *task_filters())
    findings_by_severity = await _counts(session, ReviewFinding.severity, *finding_where())
    findings_by_category = await _counts(session, ReviewFinding.category, *finding_where())
    provider_split = await _counts(session, ReviewTask.provider, *task_filters())
    tasks_by_mode = await _counts(
        session, ReviewTask.exec_mode, ReviewTask.exec_mode.isnot(None), *task_filters()
    )  # 模式饼恒排除 NULL；项目隔离下仅算成员项目

    # Agent 阶段管线分布：diff 无对话，天然只含 agent；项目隔离下仅成员项目
    phase_box = await phase_box_stats(session, task_filter=task_filter)

    # agent 实际执行任务数 + 平均对话轮数（KPI；恒 agent）
    agr = select(
        func.count(),
        func.coalesce(func.avg(ReviewTask.chat_rounds), 0),
    ).where(ReviewTask.exec_mode == "agentic")
    for w in rbac_filters():
        agr = agr.where(w)
    agent_row = (await session.execute(agr)).one()
    agent_task_count = int(agent_row[0] or 0)
    avg_chat_rounds = round(float(agent_row[1] or 0), 2)

    # 复杂度×成本散点：恒 agent（完成、时间戳齐全；duration 不含排队）
    scatter = select(
        ReviewTask.diff_lines, ReviewTask.started_at, ReviewTask.finished_at,
        ReviewTask.chat_rounds, ReviewTask.tool_calls,
    ).where(
        ReviewTask.exec_mode == "agentic",
        ReviewTask.state == "completed",
        ReviewTask.started_at.isnot(None),
        ReviewTask.finished_at.isnot(None),
    )
    for w in rbac_filters():
        scatter = scatter.where(w)
    scatter_rows = (await session.execute(
        scatter.order_by(ReviewTask.id.desc()).limit(200)
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

    # 近 14 天每日审查量（服务端补零，缺失日期填 0；mode/RBAC 可过滤）
    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=13)
    by_day_stmt = select(func.date(ReviewTask.queued_at), func.count().label("n"))
    for w in task_filters():
        by_day_stmt = by_day_stmt.where(w)
    by_day_rows = (await session.execute(
        by_day_stmt.where(ReviewTask.queued_at >= start_day)
        .group_by(func.date(ReviewTask.queued_at))
    )).all()
    by_day = {str(r[0]): int(r[1]) for r in by_day_rows if r[0] is not None}
    reviews_by_day = [
        ReviewsByDay(day=str(start_day + timedelta(days=i)),
                     count=by_day.get(str(start_day + timedelta(days=i)), 0))
        for i in range(14)
    ]

    # 近 14 天单任务平均耗时（按完成日分桶；mode/RBAC 可过滤）
    dur_stmt = select(ReviewTask.started_at, ReviewTask.finished_at)
    for w in task_filters():
        dur_stmt = dur_stmt.where(w)
    dur_rows = (await session.execute(
        dur_stmt.where(
            ReviewTask.state == "completed",
            ReviewTask.started_at.isnot(None),
            ReviewTask.finished_at.isnot(None),
            ReviewTask.finished_at >= start_day,
        )
    )).all()
    dur_by_day: dict[str, list[float]] = {}
    for starts, fin in dur_rows:
        if starts is None or fin is None:
            continue
        secs = (fin - starts).total_seconds()
        if secs < 0:
            continue
        # DB 存 UTC、SQLite 读回是 naive（本地墙钟即 UTC）→ 按 UTC 取完成日分桶
        finish = fin if fin.tzinfo is not None else fin.replace(tzinfo=UTC)
        dur_by_day.setdefault(finish.date().isoformat(), []).append(secs)
    duration_by_day: list[DailyDurationItem] = []
    for i in range(14):
        day = str(start_day + timedelta(days=i))
        arr = dur_by_day.get(day, [])
        duration_by_day.append(DailyDurationItem(
            day=day, count=len(arr),
            avg_seconds=round(sum(arr) / len(arr), 1) if arr else 0.0,
        ))

    # 模型 token 用量（mode 经 task 关联过滤；RBAC 同）
    usage = select(
        ModelUsage.model,
        func.count().label("reqs"),
        func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
        func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
        func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        func.coalesce(func.sum(ModelUsage.cost), 0),
    )
    if mode_subq is not None:
        usage = usage.where(ModelUsage.task_id.in_(mode_subq))
    if task_subq is not None:
        usage = usage.where(ModelUsage.task_id.in_(task_subq))
    usage_rows = (await session.execute(usage.group_by(ModelUsage.model))).all()
    model_usage = [
        ModelUsageItem(model=str(r[0] or "未知"), requests=int(r[1]),
                       prompt_tokens=int(r[2]), completion_tokens=int(r[3]),
                       total_tokens=int(r[4]), cost=round(float(r[5] or 0), 4))
        for r in usage_rows
    ]

    # 近 14 天每日成本趋势（服务端补零；mode 经 task 关联过滤；RBAC 同）
    cost_stmt = select(
        func.date(ModelUsage.ts),
        func.coalesce(func.sum(ModelUsage.cost), 0),
    )
    if mode_subq is not None:
        cost_stmt = cost_stmt.where(ModelUsage.task_id.in_(mode_subq))
    if task_subq is not None:
        cost_stmt = cost_stmt.where(ModelUsage.task_id.in_(task_subq))
    cost_rows = (await session.execute(
        cost_stmt.where(ModelUsage.ts >= start_day).group_by(func.date(ModelUsage.ts))
    )).all()
    cost_by_day_map = {str(r[0]): float(r[1]) for r in cost_rows if r[0] is not None}
    cost_by_day = [
        DailyCostItem(day=str(start_day + timedelta(days=i)),
                      cost=round(cost_by_day_map.get(str(start_day + timedelta(days=i)), 0.0), 4))
        for i in range(14)
    ]

    return StatsOut(
        total_tasks=total_tasks, total_findings=total_findings,
        open_critical=open_critical, open_high=open_high,
        tasks_by_state=tasks_by_state, findings_by_severity=findings_by_severity,
        findings_by_category=findings_by_category, reviews_by_day=reviews_by_day,
        model_usage=model_usage, cost_by_day=cost_by_day, duration_by_day=duration_by_day,
        phase_box=phase_box, provider_split=provider_split,
        tasks_by_mode=tasks_by_mode, agent_task_count=agent_task_count,
        avg_chat_rounds=avg_chat_rounds, agent_scatter=agent_scatter,
    )


@router.get("", response_model=StatsOut)
async def get_stats(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    mode: str = "all",
) -> StatsOut:
    """聚合全部看板指标；`mode` 按 exec_mode 过滤（all/agentic/diff）；按用户成员关系做项目隔离。"""
    if mode not in ("all", "agentic", "diff"):
        mode = "all"
    is_global, ids = await allowed_project_ids(session, user)
    task_filter = review_scope_clause(is_global, ids)
    return await aggregate_stats(session, mode=mode, task_filter=task_filter)


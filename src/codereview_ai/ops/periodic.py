"""日报调度（DESIGN §19 / M5.7）：asyncio 定时聚合近 24h 数据 → markdown → IM 推送。

- `collect_daily_stats`：近 24h（`since ~ now`）聚合 ReviewTask / ReviewFinding /
  ModelUsage（复用 M5.4 的 GROUP BY 聚合思路，见 api.admin.stats）。
- `render_daily_report`：把聚合结果渲染成纯 markdown（无 PR 上下文的日报体）。
- `DailyReporter`：`run_once()` 聚合+推送（失败仅记日志不抛）；`run_forever(stop_event)`
  asyncio 循环按 `hour` 到点推送，随主进程生命周期清理。

离线可测：临时 SQLite 播种 → 聚合数值断言；`send_markdown` 走 MockTransport 断言 payload。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ModelUsage, ReviewFinding, ReviewTask

logger = logging.getLogger("codereview_ai.ops.periodic")


@dataclass
class DailyStats:
    """近 24h 聚合结果，供日报渲染。"""

    since: datetime
    until: datetime
    total_tasks: int = 0
    tasks_by_state: dict[str, int] = field(default_factory=dict)
    findings_by_severity: dict[str, int] = field(default_factory=dict)
    findings_by_category: dict[str, int] = field(default_factory=dict)
    model_usage: list[tuple[str, int, int]] = field(default_factory=list)  # (model, reqs, tokens)
    open_critical: int = 0
    open_high: int = 0


async def collect_daily_stats(session: AsyncSession, since: datetime) -> DailyStats:
    """聚合 `since ~ now` 的审查任务 / finding / 用量，复刻 M5.4 看板聚合。"""
    until = datetime.now(UTC)

    async def by_state() -> dict[str, int]:
        rows = await session.execute(
            select(ReviewTask.state, func.count()).where(ReviewTask.queued_at >= since)
            .group_by(ReviewTask.state)
        )
        return {str(k or "未知"): int(v) for k, v in rows.all()}

    async def by_severity() -> dict[str, int]:
        rows = await session.execute(
            select(ReviewFinding.severity, func.count()).where(ReviewFinding.first_seen >= since)
            .group_by(ReviewFinding.severity)
        )
        return {str(k or "未知"): int(v) for k, v in rows.all()}

    async def by_category() -> dict[str, int]:
        rows = await session.execute(
            select(ReviewFinding.category, func.count()).where(ReviewFinding.first_seen >= since)
            .group_by(ReviewFinding.category)
        )
        return {str(k or "未知"): int(v) for k, v in rows.all()}

    async def usage() -> list[tuple[str, int, int]]:
        rows = await session.execute(
            select(
                ModelUsage.model,
                func.count().label("reqs"),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
            ).where(ModelUsage.ts >= since).group_by(ModelUsage.model)
        )
        return [(str(r[0] or "未知"), int(r[1]), int(r[2])) for r in rows.all()]

    total_tasks = int((await session.execute(
        select(func.count()).select_from(ReviewTask).where(ReviewTask.queued_at >= since)
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

    return DailyStats(
        since=since, until=until, total_tasks=total_tasks,
        tasks_by_state=await by_state(), findings_by_severity=await by_severity(),
        findings_by_category=await by_category(), model_usage=await usage(),
        open_critical=open_critical, open_high=open_high,
    )


def render_daily_report(s: DailyStats) -> str:
    """把 DailyStats 渲染成 markdown 日报体（顶层标题 + 计数 + 表格，无 PR 属主）。"""
    window = (
        f"- 统计窗口：{s.since.strftime('%m-%d %H:%M')} ~ "
        f"{s.until.strftime('%m-%d %H:%M')}（近 24h，UTC）"
    )
    lines = [
        "## 📊 代码审查日报",
        "",
        window,
        f"- 审查任务：**{s.total_tasks}** 次",
        f"- 待解决高危 / 严重：**{s.open_high}** / **{s.open_critical}**",
        "",
        "### 任务状态",
        _count_table("状态", "数量", s.tasks_by_state),
        "",
        "### 问题严重级分布",
        _count_table("严重级", "数量", s.findings_by_severity),
        "",
        "### 问题类别分布",
        _count_table("类别", "数量", s.findings_by_category),
        "",
        "### 模型用量",
        _markdown_table([("模型", "请求数", "token"), *(_usage_row(u) for u in s.model_usage)]),
    ]
    return "\n".join(lines)


def _count_table(kind: str, value: str, counts: dict[str, int]) -> str:
    """把 dict 计数渲染成按数量降序的表格。"""
    rows: Sequence[Sequence[object]] = [(kind, value)]
    rows = [*rows, *sorted(counts.items(), key=lambda pair: -pair[1])]
    return _markdown_table(rows)


def _usage_row(u: tuple[str, int, int]) -> tuple[str, str, str]:
    return u[0], str(u[1]), str(u[2])


def _markdown_table(rows: Sequence[Sequence[object]]) -> str:
    """渲染 markdown 表格；仅表头（无数据行）时返回占位文本。"""
    if not rows or len(rows) <= 1:
        return "（无数据）"
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    header = "| " + " | ".join(str(r).ljust(widths[i]) for i, r in enumerate(rows[0])) + " |"
    sep = "| " + " | ".join("-" * widths[i] for i in range(len(rows[0]))) + " |"
    body = "\n".join(
        "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) + " |"
        for row in rows[1:]
    )
    return header + "\n" + sep + ("\n" + body if body else "")


def _seconds_until_hour(hour: int, *, now: datetime | None = None) -> float:
    """距下一次本地时区 `hour` 点的秒数（今天该点已过 → 明天）。"""
    local = (now or datetime.now()).replace(minute=0, second=0, microsecond=0)
    target = local.replace(hour=hour)
    if target <= local:
        target = target + timedelta(days=1)
    return (target - local).total_seconds()


class DailyReporter:
    """按配置时刻聚合并推送日报；失败仅记日志，不炸进程（M5.7 §19）。"""

    def __init__(
        self,
        engine: AsyncEngine,
        dispatcher: NotifierDispatcher,
        *,
        hour: int = 9,
        enabled: bool = True,
        window_hours: int = 24,
    ) -> None:
        self._engine = engine
        self._dispatcher = dispatcher
        self._hour = max(0, min(23, hour))
        self._enabled = enabled
        self._window_hours = window_hours

    async def run_once(self) -> str:
        """聚合近 24h → 渲染 → 推送到全局路由；失败记日志，返回渲染后的 markdown/text。"""
        since = datetime.now(UTC) - timedelta(hours=self._window_hours)
        try:
            session = session_factory(self._engine)
            async with session() as s:
                stats = await collect_daily_stats(s, since)
        except Exception as exc:  # noqa: BLE001  日报失败不炸进程
            logger.error("日报聚合失败：%s", exc, exc_info=True)
            return ""
        markdown = render_daily_report(stats)
        try:
            await self._dispatcher.send_markdown("代码审查日报", markdown)
        except Exception as exc:  # noqa: BLE001
            logger.error("日报推送失败：%s", exc, exc_info=True)
        return markdown

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """调度循环：到点推进日报，直到 stop_event（随 worker 生命周期清理）。"""
        while not stop_event.is_set():
            wait = _seconds_until_hour(self._hour)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
                break  # stop_event 置位 → 退出
            except TimeoutError:
                if self._enabled:
                    logger.info("日报到点，启动聚合推送")
                    await self.run_once()

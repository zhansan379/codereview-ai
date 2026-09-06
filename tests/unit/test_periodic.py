"""日报（M5.7）离线单测：聚合近 24h + markdown 渲染 + 调度到点 + 推送 payload。

- collect_daily_stats：临时 SQLite 播近 24h / 24h 前数据，断言只统计窗口内。
- render_daily_report：断言标题/计数/表格骨架。
- _seconds_until_hour：今天该点已过 → 明天。
- DailyReporter.run_once：MockTransport 断言日报推送到账（含「日报」字样）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from codereview_ai.config.repository import NotifierRoute
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.ops.periodic import (
    DailyReporter,
    _seconds_until_hour,
    collect_daily_stats,
    render_daily_report,
)
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ModelUsage, ReviewFinding, ReviewTask


async def _seed_db(engine) -> None:
    """播 2 个在窗口内 + 1 个窗口外任务、finding、用量。"""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    async with session_factory(engine)() as s:
        s.add_all([
            ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                       head_sha="a1", state="completed"),
            ReviewTask(provider="gitlab", repo_id="2", pr_number=2, event_type="mr",
                       head_sha="a2", state="failed"),
            ReviewTask(provider="github", repo_id="3", pr_number=None, event_type="push",
                       head_sha="a3", state="completed", queued_at=old),
        ])
        await s.commit()
        t1 = (await s.execute(
            select(ReviewTask).where(ReviewTask.head_sha == "a1")
        )).scalar_one()
        s.add_all([
            ReviewFinding(task_id=t1.id, fingerprint="f1", severity="high", category="bug",
                          status="active"),
            ReviewFinding(task_id=t1.id, fingerprint="f2", severity="critical",
                          category="security", status="active"),
            ReviewFinding(task_id=t1.id, fingerprint="f3", severity="high", category="bug",
                          status="active", first_seen=old),  # 窗口外不计
        ])
        s.add_all([
            ModelUsage(task_id=1, model="gpt-4o", total_tokens=150),
            ModelUsage(task_id=2, model="claude", total_tokens=15),
            ModelUsage(task_id=3, model="gpt-4o", total_tokens=999, ts=old),  # 窗口外不计
        ])
        await s.commit()


async def test_collect_daily_stats_window(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    await init_db(engine)
    await _seed_db(engine)
    async with session_factory(engine)() as s:
        stats = await collect_daily_stats(s, datetime.now(UTC) - timedelta(hours=24))
    assert stats.total_tasks == 2  # 窗口外 push 任务不计
    assert stats.tasks_by_state["completed"] == 1
    assert stats.tasks_by_state["failed"] == 1
    assert stats.findings_by_severity["high"] == 1  # 窗口外 high 不计
    assert stats.findings_by_severity["critical"] == 1
    assert stats.open_critical == 1
    assert set(stats.model_usage) == {("gpt-4o", 1, 150), ("claude", 1, 15)}


def test_render_daily_report():
    s = render_daily_report(_stats())
    assert "代码审查日报" in s
    assert "审查任务：**2**" in s
    assert "### 任务状态" in s
    assert "模型" in s and "请求数" in s and "token" in s


def test_render_empty_report():
    s = render_daily_report(_stats(empty=True))
    assert "代码审查日报" in s
    assert s.count("（无数据）") >= 1


def _stats(*, empty: bool = False):
    from codereview_ai.ops.periodic import DailyStats

    if empty:
        return DailyStats(
            since=datetime(2020, 1, 1, tzinfo=UTC), until=datetime(2020, 1, 2, tzinfo=UTC),
        )
    return DailyStats(
        since=datetime(2020, 1, 1, tzinfo=UTC), until=datetime(2020, 1, 2, tzinfo=UTC),
        total_tasks=2, tasks_by_state={"completed": 1, "failed": 1},
        findings_by_severity={"high": 1, "critical": 1},
        model_usage=[("gpt-4o", 1, 150)],
        open_critical=1, open_high=1,
    )


def test_seconds_until_hour_next_day():
    """今天该点已过 → 差值为明天同点。"""
    now = datetime(2024, 1, 1, 10, 30)
    delta = _seconds_until_hour(9, now=now)
    assert delta == 82800  # 10:00 → 次日 09:00：23h


def test_seconds_until_hour_today():
    now = datetime(2024, 1, 1, 8, 30)
    assert _seconds_until_hour(9, now=now) == 3600  # 08:00 → 09:00


async def _capture_collector(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"errcode": 0})

    return handler


async def test_reporter_run_once_dispatches(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'd2.db'}")
    await init_db(engine)
    await _seed_db(engine)
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [NotifierRoute(channel="dingtalk", webhook="https://w/x",
                              secret="", project_id=None, at_threshold=60)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        reporter = DailyReporter(engine, disp, hour=9)
        markdown = await reporter.run_once()
    assert captured, "日报应推送到账"
    assert "日报" in json.dumps(captured[0].content.decode("utf-8")) or "日报" in _text(captured[0])
    assert "代码审查日报" in markdown
    await engine.dispose()


def _text(req: httpx.Request) -> str:
    try:
        return json.dumps(json.loads(req.content.decode("utf-8")), ensure_ascii=False)
    except ValueError:
        return req.content.decode("utf-8")

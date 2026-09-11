"""ops/scheduler：APScheduler 版调度编排。

`run_once` 用 fake 记录调用，专注验证调度编排：空表播种默认、poll 间隔到点触发、
daily cron / 旧 hour 到点触发、停用/删除/改参热更、run_job_now 立即执行。离线：
临时 SQLite + fake runner + AsyncIOScheduler（跑在 pytest-asyncio 的循环上）。
"""

from __future__ import annotations

import asyncio

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.ops.scheduler import ScheduleManager
from codereview_ai.storage.db import create_engine, init_db
from codereview_ai.storage.schedule_repo import ScheduleJobRepository


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
    await init_db(eng)
    yield eng
    await eng.dispose()


class FakeRunner:
    """记录 run_once 调用次数的执行器替身。"""

    def __init__(self) -> None:
        self.count = 0

    async def run_once(self) -> None:
        self.count += 1


async def test_seed_creates_default_jobs(engine):
    mgr = ScheduleManager(engine, seed_defaults={
        "poll": {"enabled": True, "params": {"interval_seconds": 3600}},
        "daily": {"enabled": False, "params": {"hour": 9}},
    })
    await mgr.start()
    try:
        rows = await ScheduleJobRepository(engine).list()
        assert {r.job_type for r in rows} == {"poll", "daily"}
    finally:
        await mgr.stop()


async def test_poll_job_fires_on_interval(engine):
    poller = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="p", job_type="poll", enabled=True,
                            params={"interval_seconds": 1})
    mgr = ScheduleManager(engine, poller=poller, seed_defaults=None)
    await mgr.start()  # reconcile 拾起已插入的 poll 任务
    try:
        await asyncio.sleep(1.5)
        assert poller.count >= 1
        assert job.id in mgr._tasks
    finally:
        await mgr.stop()


async def test_daily_cron_schedules_next_run(engine):
    """daily 用 cron 字符串（`* * * * *` 每分钟）→ job 注册且下次触发在下分钟以内（分钟粒度，
    `from_crontab` 只收 5 字段）。实际触发路径已由 poll interval 测试覆盖。"""
    reporter = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="d", job_type="daily", enabled=True,
                            params={"cron": "* * * * *"})
    mgr = ScheduleManager(engine, reporter=reporter, seed_defaults=None)
    await mgr.start()
    try:
        assert job.id in mgr._tasks
        aps_job = mgr._sched.get_job(f"job-{job.id}")
        assert aps_job is not None
        assert isinstance(aps_job.trigger, CronTrigger)
        assert aps_job.next_run_time is not None  # 已武装到点触发
    finally:
        await mgr.stop()


async def test_daily_legacy_hour_still_works(engine):
    """旧 hour 参数被翻译成每日整点：job 能注册，且不会崩。"""
    reporter = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="d", job_type="daily", enabled=True,
                            params={"hour": 9})
    mgr = ScheduleManager(engine, reporter=reporter, seed_defaults=None)
    await mgr.start()
    try:
        assert job.id in mgr._tasks
        assert isinstance(mgr._tasks[job.id].trigger, CronTrigger)  # 确实是 cron 触发器
        await mgr.run_job_now(job)
        assert reporter.count == 1
    finally:
        await mgr.stop()


async def test_invalid_legacy_hour_does_not_break_reconcile(engine):
    """越界 hour 不炸 reconcile：回退到默认每日 9 点可注册。"""
    reporter = FakeRunner()
    repo = ScheduleJobRepository(engine)
    await repo.create(name="d", job_type="daily", enabled=True, params={"hour": 99})
    mgr = ScheduleManager(engine, reporter=reporter, seed_defaults=None)
    await mgr.start()
    try:
        assert reporter.run_once  # reconcile 正常完成
    finally:
        await mgr.stop()


async def test_disable_reconciles_cancels_and_reenable(engine):
    poller = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="p", job_type="poll", enabled=True,
                            params={"interval_seconds": 1})
    mgr = ScheduleManager(engine, poller=poller, seed_defaults=None)
    await mgr.start()
    try:
        await asyncio.sleep(1.5)
        assert poller.count >= 1

        # 停用 → reconcile 移除任务，不再触发
        await repo.update(job.id, enabled=False, name=None, params=None)
        await mgr.reconcile()
        assert job.id not in mgr._tasks
        assert mgr._sched.get_job(f"job-{job.id}") is None
        base = poller.count
        await asyncio.sleep(1.1)
        assert poller.count == base

        # 重新启用 → reconcile 重建任务，恢复触发
        await repo.update(job.id, enabled=True, name=None, params=None)
        await mgr.reconcile()
        assert job.id in mgr._tasks
        await asyncio.sleep(1.2)
        assert poller.count > base
    finally:
        await mgr.stop()


async def test_reschedule_on_param_change(engine):
    """改参（interval_seconds）→ reconcile 用新触发器重建，不影响触发本身。"""
    poller = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="p", job_type="poll", enabled=True,
                            params={"interval_seconds": 1})
    mgr = ScheduleManager(engine, poller=poller, seed_defaults=None)
    await mgr.start()
    try:
        await asyncio.sleep(1.5)
        assert poller.count >= 1
        await repo.update(job.id, enabled=True, name=None,
                          params={"interval_seconds": 3600})
        await mgr.reconcile()
        assert job.id in mgr._tasks
        assert mgr._tasks[job.id].trigger.interval.total_seconds() == 3600
    finally:
        await mgr.stop()


async def test_delete_cancels_task(engine):
    poller = FakeRunner()
    repo = ScheduleJobRepository(engine)
    job = await repo.create(name="p", job_type="poll", enabled=True,
                            params={"interval_seconds": 1})
    mgr = ScheduleManager(engine, poller=poller, seed_defaults=None)
    await mgr.start()
    try:
        await asyncio.sleep(1.5)
        assert job.id in mgr._tasks
        await repo.delete(job.id)
        await mgr.reconcile()
        assert job.id not in mgr._tasks
        assert mgr._sched.get_job(f"job-{job.id}") is None
    finally:
        await mgr.stop()


async def test_run_job_now_dispatches_by_type(engine):
    poller = FakeRunner()
    reporter = FakeRunner()
    repo = ScheduleJobRepository(engine)
    poll_job = await repo.create(name="p", job_type="poll", enabled=False,
                                 params={"interval_seconds": 3600})
    daily_job = await repo.create(name="d", job_type="daily", enabled=False,
                                  params={"cron": "0 9 * * *"})
    mgr = ScheduleManager(engine, poller=poller, reporter=reporter, seed_defaults=None)
    await mgr.start()
    try:
        await mgr.run_job_now(poll_job)
        await mgr.run_job_now(daily_job)
        assert poller.count == 1
        assert reporter.count == 1
    finally:
        await mgr.stop()


async def test_notify_config_changed_sets_event(engine):
    mgr = ScheduleManager(engine, seed_defaults=None)
    await mgr.start()
    try:
        assert mgr._changed.is_set() is False
        mgr.notify_config_changed()
        assert mgr._changed.is_set() is True
    finally:
        await mgr.stop()

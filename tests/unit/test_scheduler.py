"""ops/scheduler：按 schedule_job 表驱动定时任务。

`run_once` 用 fake 记录调用，专注验证调度编排：空表播种默认、enabled 任务到点触发、
停用/删除/重启用 reconcile 热更、run_job_now 立即执行。离线：临时 SQLite + fake runner。
"""

from __future__ import annotations

import asyncio

import pytest
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

        # 停用 → reconcile 取消任务，不再触发
        await repo.update(job.id, enabled=False, name=None, params=None)
        await mgr.reconcile()
        assert job.id not in mgr._tasks
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
    finally:
        await mgr.stop()


async def test_run_job_now_dispatches_by_type(engine):
    poller = FakeRunner()
    reporter = FakeRunner()
    repo = ScheduleJobRepository(engine)
    poll_job = await repo.create(name="p", job_type="poll", enabled=False,
                                 params={"interval_seconds": 3600})
    daily_job = await repo.create(name="d", job_type="daily", enabled=False,
                                  params={"hour": 9})
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

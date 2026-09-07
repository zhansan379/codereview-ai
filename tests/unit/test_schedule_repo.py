"""storage/schedule_repo：schedule_job 表 CRUD roundtrip（无 TTL，纯增查改删）。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.schedule_repo import ScheduleJobRepository


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched_repo.db'}")
    await init_db(eng)
    yield eng
    await eng.dispose()


async def test_crud_roundtrip(engine):
    repo = ScheduleJobRepository(engine)
    assert await repo.list() == []

    j = await repo.create(name="每日补拉", job_type="poll", enabled=True,
                          params={"interval_seconds": 60})
    assert j.id is not None
    assert await repo.get(j.id) is not None

    rows = await repo.list()
    assert len(rows) == 1 and rows[0].name == "每日补拉" and rows[0].job_type == "poll"

    upd = await repo.update(j.id, name="改为半小时", enabled=False,
                            params={"interval_seconds": 1800})
    assert upd is not None
    assert upd.enabled is False and upd.params == {"interval_seconds": 1800}

    assert await repo.update(999, name="x", enabled=True, params={}) is None  # 不存在
    assert await repo.delete(j.id) is True
    assert await repo.list() == []
    assert await repo.delete(j.id) is False  # 已删


async def test_repo_name_unique(engine):
    repo = ScheduleJobRepository(engine)
    await repo.create(name="p", job_type="poll", enabled=True, params={})
    from codereview_ai.storage.models import ScheduleJob
    session = session_factory(engine)
    async with session() as s:
        s.add(ScheduleJob(name="p", job_type="poll", enabled=True, params={}))
        with pytest.raises(IntegrityError):
            await s.commit()

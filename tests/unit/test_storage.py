"""storage 测试：SQLite pragma 加固、建表、读写、部分唯一索引幂等。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import create_engine, ensure_async_url, init_db, session_factory
from codereview_ai.storage.models import ReviewTask


def test_ensure_async_url_translates_dialects():
    assert ensure_async_url("sqlite:///./data/app.db") == "sqlite+aiosqlite:///./data/app.db"
    assert ensure_async_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert ensure_async_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def test_sqlite_pragmas_applied(engine):
    async with engine.connect() as conn:
        row = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        assert row == 1


async def test_tables_created(engine):
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert {"project", "review_task", "review_finding"} <= set(tables)


async def test_insert_and_read_review_task(engine):
    session = session_factory(engine)
    async with session() as s:
        s.add(
            ReviewTask(
                provider="gitlab", repo_id="123", event_type="mr",
                branch="feat/x", head_sha="abc123", base_sha="def456",
                state="queued", trace_id="t-1",
            )
        )
        await s.commit()

    async with session() as s:
        tasks = (await s.execute(select(ReviewTask))).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].state == "queued"
        assert tasks[0].head_sha == "abc123"


async def test_partial_unique_index_enforces_idempotency(engine):
    session = session_factory(engine)
    args = dict(provider="github", repo_id="o/r", event_type="mr", branch="f", head_sha="h")

    async with session() as s:
        s.add(ReviewTask(pr_number=1, **args))
        await s.commit()

    # 同幂等键（同 head_sha）第二次插入 → IntegrityError
    async with session() as s:
        s.add(ReviewTask(pr_number=1, **args))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_mr_and_push_keys_do_not_interfere(engine):
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider="gitlab", repo_id="123", event_type="mr", pr_number=7,
                         branch="f", head_sha="h1"))
        # push 轨允许 pr_number 为 NULL，且不受 mr 轨唯一性误伤
        s.add(ReviewTask(provider="gitlab", repo_id="123", event_type="push", pr_number=None,
                         branch="main", head_sha="h1"))
        await s.commit()

    async with session() as s:
        count = (await s.execute(text("SELECT COUNT(*) FROM review_task"))).scalar()
        assert count == 2

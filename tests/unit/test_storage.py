"""storage 测试：SQLite pragma 加固、建表、读写、部分唯一索引幂等。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import create_engine, ensure_async_url, init_db, session_factory
from codereview_ai.storage.models import (
    ModelConfig,
    ModelUsage,
    NotifierConfig,
    ProjectRule,
    ReviewTask,
)


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
    expected = {"project", "review_task", "review_finding",
                "model_config", "notifier_config", "project_rule", "model_usage"}
    assert expected <= set(tables)


async def test_schema_backfill_adds_new_columns_idempotently(tmp_path):
    """存量表缺新列时 `_ensure_latest_schema` 幂等补列，重复调用不报错、不重复加。"""
    from codereview_ai.storage.db import _ensure_latest_schema

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    # 模拟"旧 schema"表（仅建表、不含新列；create_all 不会 ALTER，故需补列）
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE project (id INTEGER PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
            "repo_id VARCHAR(255) NOT NULL, file_extensions VARCHAR(255) DEFAULT '')"))
        await conn.execute(text(
            "CREATE TABLE review_task (id INTEGER PRIMARY KEY, state VARCHAR(16) DEFAULT 'queued')"))
        await conn.execute(text(
            "CREATE TABLE notifier_config (id INTEGER PRIMARY KEY, channel VARCHAR(32) DEFAULT '')"))

    async def _cols(table: str) -> set[str]:
        async with engine.connect() as conn:
            return {r[1] for r in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()}

    assert not ({"push_enabled", "push_branch_globs", "mr_enabled"} & await _cols("project"))
    assert not ({"skip_reason", "force_rerun", "pr_title", "push_commits"} & await _cols("review_task"))
    # 存量表不含新列（at_all 是新增的 @所有人 开关，这里制造"旧 schema"无该列）
    assert not ({"at_all", "at_targets"} & await _cols("notifier_config"))

    await _ensure_latest_schema(engine)
    await _ensure_latest_schema(engine)  # 幂等：再跑一次不炸、不加重复列
    assert {"push_enabled", "push_branch_globs", "mr_enabled"} <= await _cols("project")
    assert {"skip_reason", "force_rerun", "pr_title", "push_commits"} <= await _cols("review_task")
    # at_all 会被补列；at_targets（旧方案的列）并无此列、也不应凭空出现
    assert {"at_all"} <= await _cols("notifier_config")
    assert not ({"at_targets"} & await _cols("notifier_config"))
    await engine.dispose()


async def test_model_config_insert_read(engine):
    session = session_factory(engine)
    async with session() as s:
        s.add(ModelConfig(name="deepseek", provider="deepseek", model="deepseek-chat",
                          api_key_encrypted="gAAAAA...cipher", priority=10, enabled=True))
        s.add(NotifierConfig(channel="dingtalk", enabled=True,
                             webhook_encrypted="gAAAAA...wh", secret_encrypted="gAAAAA...sec",
                             project_id=None, at_threshold=60))
        s.add(ProjectRule(project_id=1, path_glob="src/**", rule_text="注意并发", priority=5))
        s.add(ModelUsage(task_id=1, phase="review", model="deepseek-chat", total_tokens=120))
        await s.commit()

    async with session() as s:
        m = (await s.execute(select(ModelConfig))).scalar_one()
        n = (await s.execute(select(NotifierConfig))).scalar_one()
        r = (await s.execute(select(ProjectRule))).scalar_one()
        u = (await s.execute(select(ModelUsage))).scalar_one()
        assert m.model == "deepseek-chat" and m.api_key_encrypted.startswith("gAAAAA")
        assert n.channel == "dingtalk" and n.project_id is None
        assert r.path_glob == "src/**" and r.rule_text == "注意并发"
        assert u.total_tokens == 120


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

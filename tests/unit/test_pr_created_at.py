"""review_task.pr_created_at：落库提取 + 提交分析取时优先级 + 存量库补列。

- `ensure_task` 从 webhook payload 提取 `pull_request/object_attributes.created_at`
  落列；显式参数优先；push 轨不提取。
- workrate MR 轨取时：pr_created_at 列 → payload.created_at → queued_at 兜底。
- 存量库（无 pr_created_at 列的旧 schema）启动 `init_db` 幂等补列后可正常写入。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa

from codereview_ai.api.admin import workrate
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository

_GITHUB_PAYLOAD = json.dumps({
    "action": "opened",
    "pull_request": {"number": 7, "created_at": "2026-09-12T14:33:10Z"},
})
_GITLAB_PAYLOAD = json.dumps({
    "object_kind": "merge_request",
    "object_attributes": {"iid": 8, "created_at": "2026-09-12T11:17:07+08:00"},
})


async def test_ensure_task_extracts_pr_created_at_from_payload(tmp_path):
    """mr 轨：webhook payload 里的平台创建时间自动落列（GitHub Z / GitLab 带偏移）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    gh = await repo.ensure_task(
        provider="github", repo_id="9", pr_number=7, event_type="mr", branch="f",
        head_sha="h1", payload=_GITHUB_PAYLOAD,
    )
    gl = await repo.ensure_task(
        provider="gitlab", repo_id="9", pr_number=8, event_type="mr", branch="f",
        head_sha="h2", payload=_GITLAB_PAYLOAD,
    )
    async with session_factory(engine)() as s:
        rows = (await s.execute(
            sa.select(ReviewTask).order_by(ReviewTask.id)
        )).scalars().all()
    assert rows[0].id == gh
    # SQLite DateTime 不保留 tzinfo：读回为 naive UTC（生产侧 _as_utc_naive 归一）
    assert rows[0].pr_created_at == datetime(2026, 9, 12, 14, 33, 10)
    # GitLab +08:00 偏移 → UTC
    assert rows[1].id == gl
    assert rows[1].pr_created_at == datetime(2026, 9, 12, 3, 17, 7)
    await engine.dispose()


async def test_ensure_task_explicit_pr_created_at_wins(tmp_path):
    """显式参数优先于 payload（补拉通道直传 API 值）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    explicit = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    tid = await repo.ensure_task(
        provider="github", repo_id="9", pr_number=7, event_type="mr", branch="f",
        head_sha="h1", payload=_GITHUB_PAYLOAD, pr_created_at=explicit,
    )
    async with session_factory(engine)() as s:
        row = (await s.execute(
            sa.select(ReviewTask).where(ReviewTask.id == tid)
        )).scalars().one()
    # SQLite 读回 naive UTC（tzinfo 不持久化），与显式值同瞬时即可
    assert row.pr_created_at == datetime(2026, 9, 1, 0, 0)
    await engine.dispose()


async def test_ensure_task_push_track_not_extracted(tmp_path):
    """push 轨不提取（其时间来自 payload.commits[].timestamp，分析侧已单独展开）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await repo.ensure_task(
        provider="github", repo_id="9", pr_number=None, event_type="push",
        branch="main", head_sha="h1", payload=_GITHUB_PAYLOAD,
    )
    async with session_factory(engine)() as s:
        row = (await s.execute(
            sa.select(ReviewTask).where(ReviewTask.id == tid)
        )).scalars().one()
    assert row.pr_created_at is None
    await engine.dispose()


async def test_workrate_mr_ts_priority(tmp_path):
    """MR 轨取时：列 → payload.created_at → queued_at 兜底。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    await init_db(engine)
    payload = json.dumps({"pull_request": {"number": 2, "created_at": "2026-09-02T03:00:00Z"}})
    async with session_factory(engine)() as s:
        s.add_all([
            # 列优先：入队 12:00，真实创建 04:00 → 事件落 04:00
            ReviewTask(provider="github", repo_id="o/r", project_id=None, pr_number=1,
                       event_type="mr", branch="f", head_sha="a", state="completed",
                       pr_author="alice", queued_at=datetime(2026, 9, 1, 12, 0),
                       pr_created_at=datetime(2026, 9, 1, 4, 0)),
            # 列空、payload 有 → 用 payload 时间
            ReviewTask(provider="github", repo_id="o/r", project_id=None, pr_number=2,
                       event_type="mr", branch="f", head_sha="b", state="completed",
                       pr_author="bob", queued_at=datetime(2026, 9, 2, 12, 0),
                       payload=payload),
            # 都没有 → 退化为入队时间
            ReviewTask(provider="github", repo_id="o/r", project_id=None, pr_number=3,
                       event_type="mr", branch="f", head_sha="c", state="completed",
                       pr_author="carol", queued_at=datetime(2026, 9, 3, 8, 0)),
        ])
        await s.commit()
        events = await workrate._load_events(s, is_global=True, ids=set(), days=0)
    by_author = {e.author: e.ts for e in events}
    assert by_author["alice"] == datetime(2026, 9, 1, 4, 0)
    assert by_author["bob"] == datetime(2026, 9, 2, 3, 0)
    assert by_author["carol"] == datetime(2026, 9, 3, 8, 0)
    await engine.dispose()


async def test_init_db_adds_column_to_legacy_schema(tmp_path):
    """存量库升级：旧 schema（无 pr_created_at 列）init_db 后幂等补列，旧数据保留。"""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    # 手工建一张「旧版」review_task：除 pr_created_at 外与现模型同列（精简到写路径必需）
    old_cols = (
        "id INTEGER PRIMARY KEY", "provider VARCHAR(32) NOT NULL", "repo_id VARCHAR(255) NOT NULL",
        "project_id INTEGER", "pr_number INTEGER", "event_type VARCHAR(16) DEFAULT 'mr'",
        "branch VARCHAR(255) DEFAULT ''", "head_sha VARCHAR(64) NOT NULL",
        "base_sha VARCHAR(64) DEFAULT ''", "pr_title VARCHAR(255) DEFAULT ''",
        "pr_author VARCHAR(255) DEFAULT ''", "web_url VARCHAR(1024) DEFAULT ''",
        "push_commits TEXT DEFAULT ''", "state VARCHAR(16) DEFAULT 'queued'",
        "attempt INTEGER DEFAULT 0", "queued_at DATETIME", "started_at DATETIME",
        "finished_at DATETIME", "error TEXT DEFAULT ''", "trace_id VARCHAR(64) DEFAULT ''",
        "model_config_id INTEGER", "writeback_failed BOOLEAN DEFAULT 0",
        "skip_reason VARCHAR(32) DEFAULT ''", "force_rerun BOOLEAN DEFAULT 0",
        "model_snapshot JSON DEFAULT '{}'", "diff_snapshot TEXT DEFAULT ''",
        "summary_md TEXT DEFAULT ''", "score_total INTEGER DEFAULT 0",
        "issues JSON DEFAULT '{}'", "exec_mode VARCHAR(16)",
        "diff_lines INTEGER DEFAULT 0", "chat_rounds INTEGER DEFAULT 0",
        "tool_calls INTEGER DEFAULT 0", "payload TEXT DEFAULT ''",
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"CREATE TABLE review_task ({', '.join(old_cols)})")
        await db.execute(
            "INSERT INTO review_task (provider, repo_id, head_sha) VALUES ('github', 'o/r', 'old')"
        )
        await db.commit()

    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(engine)  # 幂等补列，不重建表、不清数据
    async with session_factory(engine)() as s:
        legacy = (await s.execute(
            sa.select(ReviewTask).where(ReviewTask.head_sha == "old")
        )).scalars().one()
        assert legacy.pr_created_at is None  # 存量行列存在且为 NULL
        s.add(ReviewTask(provider="github", repo_id="o/r", pr_number=1, event_type="mr",
                         branch="f", head_sha="new",
                         pr_created_at=datetime(2026, 9, 12, 14, 33, 10, tzinfo=UTC)))
        await s.commit()
    await engine.dispose()

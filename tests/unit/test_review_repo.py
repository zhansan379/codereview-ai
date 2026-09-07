"""审查持久化仓储测试（DESIGN §7.3/M4.5）：DB 承载增量落点 + 指纹。

离线：临时 SQLite + review_task/review_finding 直插，断言 `last_ok_review` 的读取与
`decide_from_ref` 的增量/跳过决策与 DB 落点一致。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.review.increments import REASON_ALREADY, REASON_INCREMENTAL, decide_from_ref
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import EventStore, QueueEnqueuer, replay_pending_tasks


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'repo.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


def _pr(head_sha: str) -> object:
    @dataclass
    class PR:
        provider: str
        repo_id: str
        pr_number: int
        head_sha: str

    return PR("gitlab", "9", 42, head_sha)


async def _seed_ok(engine: AsyncEngine, head_sha: str, fingerprints: list[str]) -> int:
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(provider="gitlab", repo_id="9", pr_number=42, event_type="mr",
                       branch="main", head_sha=head_sha, state="completed", score_total=75)
        s.add(t)
        await s.flush()
        for fp in fingerprints:
            s.add(ReviewFinding(task_id=t.id, fingerprint=fp, severity="low",
                                category="other", file="a.py", title="t"))
        await s.commit()
        return t.id


async def test_last_ok_review_returns_head_and_fingerprints(engine):
    await _seed_ok(engine, "aaa111", ["fp-1", "fp-2"])
    repo = ReviewRepository(engine)
    ref = await repo.last_ok_review("gitlab", "9", 42)
    assert ref is not None
    assert ref.head_sha == "aaa111"
    assert ref.fingerprints == frozenset({"fp-1", "fp-2"})


async def test_last_ok_review_none_if_never_completed(engine):
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=7, event_type="mr",
                         branch="main", head_sha="x", state="failed"))
        await s.commit()
    repo = ReviewRepository(engine)
    assert await repo.last_ok_review("gitlab", "9", 7) is None
    assert await repo.last_ok_review("gitlab", "9", 999) is None  # 不存在的 PR


async def test_decide_incremental_and_already_from_db(engine):
    await _seed_ok(engine, "old-head", ["fp-of-old"])
    repo = ReviewRepository(engine)

    ref = await repo.last_ok_review("gitlab", "9", 42)

    # 新 head 且 base 链有效 → 增量
    dv = decide_from_ref(ref, _pr("new-head"), chain_valid=True)
    assert dv.reason == REASON_INCREMENTAL and dv.is_incremental is True
    assert dv.last_reviewed_sha == "old-head"

    # 同一 head 重放 → 跳过
    already = decide_from_ref(ref, _pr("old-head"), chain_valid=True)
    assert already.reason == REASON_ALREADY and already.is_incremental is False


async def test_latest_completed_wins(engine):
    await _seed_ok(engine, "older", ["a"])
    await _seed_ok(engine, "newest", ["b"])
    repo = ReviewRepository(engine)
    ref = await repo.last_ok_review("gitlab", "9", 42)
    assert ref is not None and ref.head_sha == "newest"


# ── 启动回放（崩溃恢复）──────────────────────────────────────────────────


async def test_pending_for_replay_picks_stuck_queued_and_running(engine):
    """连续拿 `queued/running` 且带 payload 的行；completed/failed/空 payload 不入选。"""
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=1, event_type="mr",
                         branch="f", head_sha="h1", state="queued", payload='{"x":1}'))
        s.add(ReviewTask(provider="github", repo_id="9", pr_number=2, event_type="mr",
                         branch="f", head_sha="h2", state="running", payload='{"y":2}'))
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=3, event_type="mr",
                         branch="f", head_sha="h3", state="completed", payload='{"z":3}'))
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=4, event_type="mr",
                         branch="f", head_sha="h4", state="failed", payload='{"a":4}'))
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=5, event_type="mr",
                         branch="f", head_sha="h5", state="queued", payload=""))
        await s.commit()

    rows = await ReviewRepository(engine).pending_for_replay()
    assert {(p, v) for _, p, v in rows} == {("gitlab", '{"x":1}'), ("github", '{"y":2}')}

    # running 崩溃残留已复位回 queued，不再悬死
    async with session() as s:
        st = (await s.execute(
            select(ReviewTask.state).where(ReviewTask.head_sha == "h2")
        )).scalar_one()
    assert st == "queued"


async def test_mark_state_running_sets_started_at(engine):
    """worker 开审跑 `mark_state(running)`：写 started_at、不写 finished_at。"""
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(provider="gitlab", repo_id="9", pr_number=7, event_type="mr",
                       branch="f", head_sha="h1", state="queued")
        s.add(t)
        await s.commit()
        tid = t.id

    repo = ReviewRepository(engine)
    await repo.mark_state(tid, state="running")

    async with session() as s:
        row = (await s.execute(select(ReviewTask).where(ReviewTask.id == tid))).scalar_one()
    assert row.state == "running"
    assert row.started_at is not None
    assert row.finished_at is None

    await repo.mark_state(tid, state="completed", summary_md="ok", score_total=80)
    async with session() as s:
        row = (await s.execute(select(ReviewTask).where(ReviewTask.id == tid))).scalar_one()
    assert row.state == "completed"
    assert row.finished_at is not None  # 收尾写 finished_at


async def test_replay_pending_tasks_requeues_into_queue(engine):
    """回放协调：把遗留 payload 重新投进内存队列，worker 重启后能真正拾取续跑。"""
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider="gitlab", repo_id="9", pr_number=7, event_type="mr",
                         branch="f", head_sha="hh", state="queued", payload='{"k":"v"}'))
        await s.commit()

    queue = AsyncioTaskQueue()
    store = EventStore()
    enqueuer = QueueEnqueuer(queue, store)
    n = await replay_pending_tasks(ReviewRepository(engine), enqueuer)

    assert n == 1
    meta = await queue.claim()
    assert meta is not None
    # payload 与 provider 已随事件重新暂存，重放可继续走完整管线
    assert store.get(meta.task_id) == ("gitlab", b'{"k":"v"}')

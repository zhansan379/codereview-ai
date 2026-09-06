"""审查持久化仓储测试（DESIGN §7.3/M4.5）：DB 承载增量落点 + 指纹。

离线：临时 SQLite + review_task/review_finding 直插，断言 `last_ok_review` 的读取与
`decide_from_ref` 的增量/跳过决策与 DB 落点一致。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.review.increments import REASON_ALREADY, REASON_INCREMENTAL, decide_from_ref
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository


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

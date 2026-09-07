"""finding 生命周期（DESIGN §7.3）：resolved 自动对账 + waived 手动端点。

- `ReviewRepository.reconcile_findings`：非增量全量轮对账四分支 + 文件覆盖门 + waived 豁免。
- `insert_findings` 的 `skip_fingerprints` 复现去重。
- 管理端点 `POST /reviews/findings/{id}/status`：waive/恢复/非法值/404。
离线：临时 SQLite + httpx ASGI。
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.admin import reviews as admin_reviews
from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.domain.models import Category, Finding, Severity
from codereview_ai.review.increments import finding_fingerprint
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository

MR = dict(provider="gitlab", repo_id="9", pr_number=7, event_type="mr", branch="f")


def _finding(content: str, file: str) -> Finding:
    return Finding(content=content, category=Category.BUG, severity=Severity.HIGH,
                   existing_code="", file=file)


async def _seed_task(engine, head_sha="h") -> int:
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(**MR, head_sha=head_sha, state="completed")
        s.add(t)
        await s.commit()
        return t.id


async def _finding_rows(engine, task_id: int) -> list[ReviewFinding]:
    session = session_factory(engine)
    async with session() as s:
        return (await s.execute(
            sa.select(ReviewFinding).where(ReviewFinding.task_id == task_id)
        )).scalars().all()


# ── insert_findings skip_fingerprints 去重 ──────────────────────────────────


async def test_insert_findings_skips_known_fingerprints(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await _seed_task(engine)
    f1, f2 = _finding("A", "a.py"), _finding("B", "b.py")

    await repo.insert_findings(tid, [f1, f2])
    assert len(await _finding_rows(engine, tid)) == 2

    skip = frozenset({finding_fingerprint(f1)})
    await repo.insert_findings(tid, [f1, _finding("C", "c.py")], skip_fingerprints=skip)
    assert len(await _finding_rows(engine, tid)) == 3  # A/B/C；A 复现被跳过
    await engine.dispose()


# ── reconcile_findings 四分支 ───────────────────────────────────────────────


async def test_reconcile_active_absent_goes_resolved(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    prev_tid = await _seed_task(engine)
    await repo.insert_findings(prev_tid, [_finding("gone", "a.py")])

    cur_task_id = await _seed_task(engine, head_sha="h2")  # 新一轮
    skip = await repo.reconcile_findings(
        provider="gitlab", repo_id="9", pr_number=7,
        current_findings=[_finding("kept", "b.py")], covered_files={"a.py", "b.py"},
        exclude_task_id=cur_task_id,
    )
    (prev,) = await _finding_rows(engine, prev_tid)
    assert prev.status == "resolved"
    assert prev.last_seen is not None
    assert skip == frozenset()
    await engine.dispose()


async def test_reconcile_not_covered_file_untouched(tmp_path):
    """file 不在本轮 covered_files → 缺席不判已解决（§7.3 保守门）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    prev_tid = await _seed_task(engine)
    await repo.insert_findings(prev_tid, [_finding("gone", "a.py")])

    cur_task_id = await _seed_task(engine, head_sha="h2")
    await repo.reconcile_findings(
        provider="gitlab", repo_id="9", pr_number=7,
        current_findings=[_finding("kept", "b.py")], covered_files={"b.py"},
        exclude_task_id=cur_task_id,
    )
    (prev,) = await _finding_rows(engine, prev_tid)
    assert prev.status == "active"
    await engine.dispose()


async def test_reconcile_present_refreshes_last_seen(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    prev_tid = await _seed_task(engine)
    await repo.insert_findings(prev_tid, [_finding("same", "a.py")])

    cur_task_id = await _seed_task(engine, head_sha="h2")
    await repo.reconcile_findings(
        provider="gitlab", repo_id="9", pr_number=7,
        current_findings=[_finding("same", "a.py")], covered_files={"a.py"},
        exclude_task_id=cur_task_id,
    )
    (prev,) = await _finding_rows(engine, prev_tid)
    assert prev.status == "active"  # 仍在 → 不转 resolved
    assert prev.reopened_count == 0
    await engine.dispose()


async def test_reconcile_resolved_reappears_reopens(tmp_path):
    """上轮 resolved、本轮复现 → 回 active + reopened_count+1，返回指纹供去重。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    prev_tid = await _seed_task(engine)
    await repo.insert_findings(prev_tid, [_finding("back", "a.py")])

    session = session_factory(engine)
    async with session() as s:
        (f,) = (await s.execute(sa.select(ReviewFinding))).scalars().all()
        f.status = "resolved"
        await s.commit()

    cur_task_id = await _seed_task(engine, head_sha="h2")
    skip = await repo.reconcile_findings(
        provider="gitlab", repo_id="9", pr_number=7,
        current_findings=[_finding("back", "a.py")], covered_files={"a.py"},
        exclude_task_id=cur_task_id,
    )
    (prev,) = await _finding_rows(engine, prev_tid)
    assert prev.status == "active"
    assert prev.reopened_count == 1
    assert finding_fingerprint(_finding("back", "a.py")) in skip
    await engine.dispose()


async def test_reconcile_waived_never_auto_changed(tmp_path):
    """waived（人工搁置）永不因缺席/复现被自动改。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    prev_tid = await _seed_task(engine)
    await repo.insert_findings(prev_tid, [_finding("ig", "a.py")])

    session = session_factory(engine)
    async with session() as s:
        (f,) = (await s.execute(sa.select(ReviewFinding))).scalars().all()
        f.status = "waived"
        await s.commit()

    cur_task_id = await _seed_task(engine, head_sha="h2")
    await repo.reconcile_findings(
        provider="gitlab", repo_id="9", pr_number=7,
        current_findings=[], covered_files={"a.py"}, exclude_task_id=cur_task_id,
    )
    (prev,) = await _finding_rows(engine, prev_tid)
    assert prev.status == "waived"
    assert prev.reopened_count == 0
    await engine.dispose()


# ── 管理端点：waive / 恢复 ──────────────────────────────────────────────────


@pytest.fixture
async def app(tmp_path):
    db_path = str(tmp_path / "life.db")
    eng = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await init_db(eng)
    settings = type("S", (), {"secret_key": "s", "encryption_key": "x" * 44})()
    fast = FastAPI()
    fast.state.engine = eng
    fast.state.settings = settings
    fast.state.config_repository = None
    fast.state.forge_registry = None
    fast.include_router(auth_router, prefix="/api")
    fast.include_router(admin_reviews.router, prefix="/api")
    token = issue_token(settings.secret_key)
    yield fast, token, db_path
    await eng.dispose()


def _seed_finding(db_path: str, status: str = "active") -> int:
    """用独立引擎在独立事件循环里 seed 一条 finding，返回其 id。"""
    async def _run():
        eng = create_engine(f"sqlite+aiosqlite:///{db_path}")
        session = session_factory(eng)
        async with session() as s:
            t = ReviewTask(**MR, head_sha="h")
            s.add(t)
            await s.flush()
            f = ReviewFinding(task_id=t.id, fingerprint="fp1", severity="high",
                              category="bug", file="a.py", status=status)
            s.add(f)
            await s.commit()
            fid = f.id
        await eng.dispose()
        return fid

    return asyncio.run(_run())


def test_finding_status_waive_and_restore(app):
    fast, token, db_path = app
    c = TestClient(fast, headers={"Authorization": f"Bearer {token}"})
    fid = _seed_finding(db_path)

    r = c.post(f"/api/reviews/findings/{fid}/status", json={"status": "waived"})
    assert r.status_code == 200
    assert r.json()["status"] == "waived"
    assert r.json()["last_seen"] is not None

    r = c.post(f"/api/reviews/findings/{fid}/status", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_finding_status_rejects_resolved_and_404(app):
    fast, token, db_path = app
    c = TestClient(fast, headers={"Authorization": f"Bearer {token}"})
    assert c.post("/api/reviews/findings/99999/status", json={"status": "resolved"}).status_code in (400, 404)
    assert c.post("/api/reviews/findings/99999/status", json={"status": "active"}).status_code == 404
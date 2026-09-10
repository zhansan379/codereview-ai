"""手动重试（POST /tasks/{id}/retry）入队行为测试。

重点：MR 轨任务若由补拉/补审触发，`ReviewTask.payload` 为空——重试若只翻 queued 不入
内存队列，任务会永卡「排队中」。应改为从审计行重建 PR 走补拉 fetch 路径（enqueue_pr）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.api.admin.tasks import retry_task
from codereview_ai.domain.models import PullRequest
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask, User
from codereview_ai.storage.seed import seed_rbac


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"
    eng = create_engine(url)
    await init_db(eng)
    async with session_factory(eng)() as s:
        await seed_rbac(s, "hunter2")  # 供直接调 handler 时传入超管 user
    yield eng
    await eng.dispose()


@pytest.fixture
async def admin(engine):
    async with session_factory(engine)() as s:
        return (await s.execute(
            select(User).where(User.username == "admin"))).scalar_one()


async def _seed(engine: AsyncEngine, *, event_type="mr", payload="", pr_number=9, **kw) -> int:
    defaults = dict(provider="gitlab", repo_id="7", event_type=event_type,
                    head_sha="a" * 40, base_sha="b" * 40,
                    web_url="https://gitlab.com/o/r/-/merge_requests/9",
                    pr_title="t", branch="feature", payload=payload, state="failed",
                    error="LLMError: boom", pr_number=pr_number)
    defaults.update(kw)
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(**defaults))
        await s.commit()
        row = (await s.execute(
            select(ReviewTask).where(ReviewTask.provider == "gitlab")
            .order_by(ReviewTask.id.desc())
        )).scalars().first()
        return row.id, row.provider, row.repo_id


class _FakeEnqueuer:
    """记录入队调用的假 enqueuer。"""

    def __init__(self) -> None:
        self.enqueued: list[str] = []      # raw bytes 路径
        self.enqueued_pr: list[PullRequest] = []

    async def enqueue(self, provider, raw) -> str:
        self.enqueued.append(provider)
        return "t-x"

    async def enqueue_pr(self, provider, pr) -> str:
        self.enqueued_pr.append(pr)
        return "t-y"


def _request(enqueuer) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(enqueuer=enqueuer)))


async def test_mr_retry_without_payload_rebuilds_pr(engine, admin):
    # 补拉入队的 MR 任务 payload 为空 → 重试重建 PR 走 enqueue_pr（补拉 fetch 路径）
    task_id, _prov, _repo = await _seed(engine)
    enq = _FakeEnqueuer()
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        assert row is not None
        out = await retry_task(row.id, _request(enq), session=s, user=admin)
        await s.refresh(row)
        assert out.state == "queued"
        assert row.state == "queued"
    assert not enq.enqueued              # 没走原始 body
    assert len(enq.enqueued_pr) == 1     # 走了补拉重建
    pr = enq.enqueued_pr[0]
    assert pr.provider == "gitlab"
    assert pr.repo_id == "7"
    assert pr.pr_number == 9
    assert pr.head_sha == "a" * 40
    assert pr.repo_full_name == "o/r"    # 从 web_url 推导
    assert pr.web_url.endswith("/merge_requests/9")


async def test_mr_retry_with_payload_uses_raw_body(engine, admin):
    task_id, _prov, _repo = await _seed(engine, payload='{"event":"x"}')
    enq = _FakeEnqueuer()
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        out = await retry_task(row.id, _request(enq), session=s, user=admin)
        assert out.state == "queued"
    assert len(enq.enqueued) == 1 and not enq.enqueued_pr


async def test_push_retry_without_payload_not_replayable(engine, admin):
    # push 轨无 payload 且非 mr → 无法重建，不入队（仅翻状态+告警）
    task_id, _prov, _repo = await _seed(engine, event_type="push", pr_number=None,
                                        payload="")
    enq = _FakeEnqueuer()
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        out = await retry_task(row.id, _request(enq), session=s, user=admin)
        assert out.state == "queued"
    assert not enq.enqueued and not enq.enqueued_pr

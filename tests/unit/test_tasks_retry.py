"""手动重试（POST /tasks/{id}/retry）入队行为测试。

重点：MR 轨任务若由补拉/补审触发，`ReviewTask.payload` 为空——重试若只翻 queued 不入
内存队列，任务会永卡「排队中」。应改为从审计行重建 PR 走补拉 fetch 路径（enqueue_pr）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import selectinload

from codereview_ai.api.admin.tasks import (
    TaskRedelivered,
    _background_redeliver,
    redeliver_task,
    retry_task,
)
from codereview_ai.domain.models import PullRequest
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import (
    Permission,
    ReviewTask,
    Role,
    RolePermission,
    SystemNotification,
    User,
)
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
    # worker_pool 非空 = 「worker 就绪」，否则 retry 会被无 worker 守卫 409 拦下
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(enqueuer=enqueuer,
                                                                    worker_pool=object())))


async def test_mr_retry_without_payload_rebuilds_pr(engine, admin):
    # 补拉入队的 MR 任务 payload 为空 → 重试重建 PR 走 enqueue_pr（补拉 fetch 路径）
    task_id, _prov, _repo = await _seed(engine, pr_author="alice", target_branch="main")
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
    assert pr.author == "alice"          # 行内 pr_author/target 随重建带上（IM 通知用）
    assert pr.target_branch == "main"
    assert pr.source_branch == "feature"
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


# ── 重新发送（POST /tasks/{id}/redeliver）───────────────────────────────


def _request_with_forge(forge=None, engine=None) -> SimpleNamespace:
    state = SimpleNamespace(forge_registry=SimpleNamespace(get=lambda _p: forge))
    if engine is not None:
        state.engine = engine
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def _seed_completed_writeback_failed(engine: AsyncEngine, *, writeback_failed=True) -> int:
    """播种一条 completed + writeback_failed 的 mr 审计行，供重发测试。"""
    return (await _seed(engine, state="completed", writeback_failed=writeback_failed,
                        summary_md="已持久化总结"))[0]


async def test_redeliver_409_when_not_writeback_failed(engine, admin):
    """writeback_failed 未置位 → 409（该任务无需重发），不触发重发。"""
    task_id = await _seed_completed_writeback_failed(engine, writeback_failed=False)
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        with pytest.raises(Exception) as exc:
            await redeliver_task(row.id, _request_with_forge(object()), user=admin, session=s)
        assert exc.value.status_code == 409  # type: ignore[attr-defined]


async def test_redeliver_409_when_forge_missing(engine, admin):
    """writeback_failed=True 但平台适配器缺失 → 409，且不落后台任务。"""
    task_id = await _seed_completed_writeback_failed(engine)
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        with pytest.raises(Exception) as exc:
            await redeliver_task(row.id, _request_with_forge(None), user=admin, session=s)
        assert exc.value.status_code == 409  # type: ignore[attr-defined]


async def _user_with_role(engine: AsyncEngine, *, all_projects: bool, codes: list[str]) -> User:
    """造一个自定义角色用户（eager load role.permissions，供 user_can 直读）。"""
    async with session_factory(engine)() as s:
        perms = (await s.execute(
            select(Permission).where(Permission.code.in_(codes)))).scalars().all()
        role = Role(name="t", description="", is_super=False,
                    is_system=False, all_projects=all_projects)
        s.add(role)
        await s.flush()
        for p in perms:
            s.add(RolePermission(role_id=role.id, permission_id=p.id))
        u = User(username="u1", password_hash="x", enabled=True, role_id=role.id)
        s.add(u)
        await s.commit()
        uid = u.id
    async with session_factory(engine)() as s:
        return (await s.execute(
            select(User).where(User.id == uid)
            .options(selectinload(User.role).selectinload(Role.permissions))
        )).scalar_one()


async def test_redeliver_404_out_of_review_scope(engine):
    """可见范围外（reviews:view 但无成员关系、非全项目角色）→ 404，不暴露任务存在。"""
    task_id = await _seed_completed_writeback_failed(engine)
    user = await _user_with_role(engine, all_projects=False, codes=["reviews:view"])
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        with pytest.raises(Exception) as exc:
            await redeliver_task(row.id, _request_with_forge(object()), user=user, session=s)
        assert exc.value.status_code == 404  # type: ignore[attr-defined]


async def test_redeliver_403_without_reviews_manage(engine):
    """可见但角色缺 reviews:manage → 403（重发与 retry/stop 同门槛）。"""
    task_id = await _seed_completed_writeback_failed(engine)
    user = await _user_with_role(engine, all_projects=True, codes=["reviews:view"])
    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        with pytest.raises(Exception) as exc:
            await redeliver_task(row.id, _request_with_forge(object()), user=user, session=s)
        assert exc.value.status_code == 403  # type: ignore[attr-defined]


async def test_redeliver_success_initiates(engine, admin, monkeypatch):
    """writeback_failed=True + forge 就绪 → 返回「已发起」，后台重发成功翻 writeback_failed=False。
    """
    task_id = await _seed_completed_writeback_failed(engine)

    class _FakeForge:
        async def fetch_pull_request(self, pr):
            return pr

        async def fetch_files(self, pr):
            return []

        async def list_comments(self, pr):
            return []

        async def post_inline(self, pr, comments):
            pass

        async def post_summary(self, pr, body):
            pass

    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        # 捕获端点 create_task 出来的真后台任务并 await 跑完再断言。此前测试手动再
        # 驱动一遍 _background_redeliver，与泄漏的 create_task 并发双跑——去重
        # (type,title) 是先查后插，并发下 TOCTOU，间歇性多落一条 redeliver_done
        # （单跑侥幸过、全量/CI 必挂的根因）。改走端点真实投递路径，无双跑。
        created: list[asyncio.Task[None]] = []
        real_create_task = asyncio.create_task

        def _capture(coro, **kwargs):
            t = real_create_task(coro, **kwargs)
            created.append(t)
            return t

        monkeypatch.setattr("codereview_ai.api.admin.tasks.asyncio.create_task", _capture)
        out = await redeliver_task(
            row.id, _request_with_forge(_FakeForge(), engine), user=admin, session=s)
        assert isinstance(out, TaskRedelivered)
        assert out.status == "redelivering"
        assert row.writeback_failed is True  # 未同步翻转（后台异步完成）
    assert created, "端点应已投递后台重发任务"
    await created[0]
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        assert row.writeback_failed is False  # 重发成功 → 归零
        notes = (await s.execute(select(SystemNotification))).scalars().all()
        assert [n.type for n in notes] == ["redeliver_done"]
        assert notes[0].level == "success"
        assert notes[0].extra_data["task_id"] == task_id


async def test_background_redeliver_failure_keeps_flag_and_notifies(engine):
    """后台重发失败 → 不抛、writeback_failed 保留（按钮仍在），且落 redeliver_failed
    系统消息（带原始报错详情，SSE 实时弹给前端）。"""
    task_id = await _seed_completed_writeback_failed(engine)

    class _BoomForge:
        async def fetch_pull_request(self, pr):
            raise RuntimeError("boom")

        async def fetch_files(self, pr):
            raise RuntimeError("boom")

    session = session_factory(engine)
    async with session() as s:
        row = (await s.execute(select(ReviewTask)
                               .where(ReviewTask.id == task_id))).scalars().first()
        from codereview_ai.storage.review_repo import ReviewRepository

        await _background_redeliver(row, _BoomForge(), ReviewRepository(engine), engine)  # type: ignore[arg-type]
        await s.refresh(row)
        assert row.writeback_failed is True  # 失败保留标记
    async with session() as s:
        notes = (await s.execute(select(SystemNotification))).scalars().all()
        assert [n.type for n in notes] == ["redeliver_failed"]
        assert notes[0].level == "error"
        assert "boom" in notes[0].message
        assert notes[0].extra_data["task_id"] == task_id

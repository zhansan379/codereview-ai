"""ops/bootstrap 懒启动测试：条件齐备现场拉起 worker、幂等短路；回放在 worker 就绪后跑。

关键回归：启动回放必须发生在 `app.state.worker_pool` 落位**之后**——回放经
enqueuer 入队要过动态 `can_run`（判 worker_pool 是否存在），若在翻转前回放，
遗留 queued 行会被误判 hold 洗成「未开始」，worker 起来后反而无人可审。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from codereview_ai.api.admin import models as admin_models
from codereview_ai.api.admin import tasks
from codereview_ai.config import Settings
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.ops.bootstrap import ensure_worker_started, worker_can_run, worker_ready
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.worker import EventStore, QueueEnqueuer
from tests.unit.helpers import make_admin_app


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


def _settings(tmp_path) -> Settings:
    # 显式钉死开关，避免本机 .env / env 的 CR_* 泄进测试（如 agent_review_enabled）
    return Settings(
        secret_key="s", webhook_secret="w", encryption_key=_fernet_key(),
        admin_password="admin", database_url=f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}",
        agent_review_enabled=False, push_review_enabled=False, poll_enabled=False,
    )


class _FakeRegistry:
    """平台适配器注册表替身：恒可用，适配器本体不被装配期调用。"""

    def available(self) -> bool:
        return True

    def get(self, provider: str) -> None:
        return None

    def providers(self) -> list[str]:
        return ["gitlab"]


class _FakeProviderRepo:
    """build_reviewer 可控的假配置仓储；notifier 路由/成员解析仅被持有不被调用。"""

    def __init__(self, reviewer: Any) -> None:
        self._reviewer = reviewer

    async def build_reviewer(self) -> Any:
        return self._reviewer

    async def notifier_routes(self, project_id: int | None = None) -> list:
        return []

    async def resolve_member_by_git_username(self, username: str, provider: str) -> None:
        return None

    async def resolve_forge(self, provider: str) -> None:
        return None


class _FakeEnqueuer:
    """只记录入队调用的替身：回放「记录」而不真投队列，杜绝 worker 消费竞争。"""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, bytes]] = []
        self.enqueued_pr: list[tuple[str, Any]] = []

    async def enqueue(self, provider: str, raw: bytes) -> str:
        self.enqueued.append((provider, raw))
        return "t-raw"

    async def enqueue_pr(self, provider: str, pr: Any) -> str:
        self.enqueued_pr.append((provider, pr))
        return "t-pr"


def _fake_app(engine, settings: Settings, tmp_path, provider_repo, enqueuer) -> Any:
    """bootstrap 所需最小 app.state：不跑真实 lifespan，避免整份 FastAPI 装配。"""
    return SimpleNamespace(state=SimpleNamespace(
        engine=engine, settings=settings, config_repository=provider_repo,
        forge_registry=_FakeRegistry(), enqueuer=enqueuer,
        queue=AsyncioTaskQueue(), event_store=EventStore(),
        cache_root=str(tmp_path / "cache"),
    ))


def _fake_reviewer() -> Any:
    return SimpleNamespace(gateway=SimpleNamespace(model="fake-model"))


@pytest.fixture
async def engine(tmp_path):
    eng = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
    await init_db(eng)
    yield eng
    await eng.dispose()


async def test_missing_state_returns_false():
    """app.state 缺基本组件（如单测假 app）→ 直接 False，无副作用。"""
    app = SimpleNamespace(state=SimpleNamespace())
    assert await ensure_worker_started(app) is False
    assert not worker_ready(app)


async def test_no_llm_returns_false_without_side_effects(engine, tmp_path):
    """LLM 解析为 None → False，不落 worker_pool/scheduler，可再次触发。"""
    app = _fake_app(engine, _settings(tmp_path), tmp_path,
                    _FakeProviderRepo(None), _FakeEnqueuer())
    assert await ensure_worker_started(app) is False
    assert not worker_ready(app)
    assert getattr(app.state, "worker_pool", None) is None
    assert getattr(app.state, "scheduler", None) is None


async def test_lazy_start_builds_worker_and_replays_after_ready(engine, tmp_path):
    """条件齐备 → 现场拉起 worker；幂等短路；回放不被动态 can_run 误判成 hold。"""
    settings = _settings(tmp_path)
    enqueuer = _FakeEnqueuer()
    async with session_factory(engine)() as s:
        # 补拉通道的典型遗留行：payload 为空的 queued mr 行（回放走 enqueue_pr）
        s.add(ReviewTask(provider="gitlab", repo_id="r", pr_number=7, event_type="mr",
                         branch="b", head_sha="h1", state="queued", payload=""))
        await s.commit()

    app = _fake_app(engine, settings, tmp_path, _FakeProviderRepo(_fake_reviewer()), enqueuer)
    assert await ensure_worker_started(app) is True
    try:
        assert worker_ready(app)
        assert app.state.worker_pool is not None
        assert app.state.scheduler is not None
        # 幂等：worker 已在跑，再次触发直接 True 短路
        assert await ensure_worker_started(app) is True
        # 回放：payload 空的 mr 行按 PR 重建走 enqueue_pr 通道
        assert len(enqueuer.enqueued_pr) == 1 and enqueuer.enqueued == []
        assert enqueuer.enqueued_pr[0][0] == "gitlab"
        assert enqueuer.enqueued_pr[0][1].pr_number == 7
        # 关键回归：回放行没有被 can_run 误判 hold 洗成「未开始」
        async with session_factory(engine)() as s:
            row = (await s.execute(select(ReviewTask))).scalar_one()
        assert row.state == "queued"
    finally:
        await app.state.worker_pool.stop()
        await app.state.scheduler.stop()
        http = getattr(app.state, "http", None)
        if http is not None:
            await http.aclose()


async def test_create_app_lifespan_starts_worker_when_configured(tmp_path, monkeypatch):
    """真实 create_app 装配冒烟：DB 预置模型+平台凭据 → lifespan 走完即有 worker。

    覆盖 bootstrap 接管启动装配后的「条件齐备」分支（其余单测覆盖的是假 app.state）。
    """
    from fastapi.testclient import TestClient

    from codereview_ai.crypto import encrypt
    from codereview_ai.main import create_app
    from codereview_ai.storage.models import ForgeConfig, ModelConfig, _utcnow

    key = _fernet_key()
    db = tmp_path / "app.db"
    settings = Settings(
        secret_key="s", webhook_secret="w", encryption_key=key, admin_password="admin",
        database_url=f"sqlite+aiosqlite:///{db}", agent_review_enabled=False,
    )
    # 预建库并落一行可用模型 + 平台凭据（env 剥离，保证走 DB 配置）
    eng = create_engine(f"sqlite+aiosqlite:///{db}")
    await init_db(eng)
    async with session_factory(eng)() as s:
        s.add(ModelConfig(name="m", provider="deepseek", model="deepseek-chat",
                          api_key_encrypted=encrypt("sk-x", key), enabled=True))
        s.add(ForgeConfig(provider="gitlab", token_encrypted=encrypt("tok", key),
                          enabled=True, created_at=_utcnow()))
        await s.commit()
    await eng.dispose()
    for var in ("CR_GITHUB_TOKEN", "CR_GITLAB_TOKEN", "CR_GITEE_TOKEN", "CR_GITEA_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    app = create_app(settings)
    with TestClient(app):
        assert worker_ready(app)
        assert app.state.scheduler is not None
        assert app.state.enqueuer.can_run() is True
        assert app.state.poller is not None


async def test_retry_endpoint_lazy_starts_after_model_saved(tmp_path, monkeypatch):
    """用户旅程回归（exe 首启场景）：无 worker 执行 409 → 保存模型即拉起 → 再执行成功。

    1. 首启未配模型：拉取/回放任务落「未开始」，点执行被 409 明确拒绝；
    2. 设置页保存模型（models API 内联懒启动）：worker 现场拉起，无需重启；
    3. 再点执行：200 入队（此前必须重启 exe 才能做到）。
    """
    import httpx

    fast, token, _admin, engine = await make_admin_app(
        tmp_path, db_name="journey.db", routers=[tasks.router, admin_models.router],
    )
    fast.state.settings = Settings(
        secret_key="s", webhook_secret="w", encryption_key=_fernet_key(),
        admin_password="admin", database_url=f"sqlite+aiosqlite:///{tmp_path / 'journey.db'}",
        agent_review_enabled=False, push_review_enabled=False, poll_enabled=False,
    )
    fast.state.config_repository = ConfigRepository(engine, encryption_key=_fernet_key())
    fast.state.forge_registry = _FakeRegistry()  # 平台凭据视为已配齐（焦点是 LLM 懒启动）
    fast.state.worker_pool = None  # 首启未配模型 → 无 worker
    fast.state.queue = AsyncioTaskQueue()
    fast.state.event_store = EventStore()
    fast.state.enqueuer = QueueEnqueuer(fast.state.queue, fast.state.event_store)
    fast.state.enqueuer.can_run = worker_can_run(fast)

    async with session_factory(engine)() as s:
        s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=11, event_type="mr",
                         branch="main", head_sha="nw1", state="skipped",
                         skip_reason="no_llm", payload='{"x":1}'))
        await s.commit()

    try:
        # ASGITransport：处理器跑在测试事件循环上（TestClient 的 portal 是独立循环，
        # 懒启动的 scheduler/worker_pool 会绑过去，测试收尾时无法在循环外优雅停止）
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fast),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            # 1) 无 worker 执行 → 409（文案不再要求「重启服务」）
            r = await c.post("/api/tasks/1/retry")
            assert r.status_code == 409
            assert "worker" in r.json()["detail"]
            assert "重启" not in r.json()["detail"]

            # 2) 保存模型 → 内联懒启动，worker 就绪
            r = await c.post("/api/models", json={
                "name": "m", "provider": "deepseek", "model": "deepseek-chat",
                "api_key": "sk-x",
            })
            assert r.status_code == 201, r.text
            assert worker_ready(fast)

            # 3) 再点执行 → 入队成功（queued/running 视 worker 认领时机）
            r = await c.post("/api/tasks/1/retry")
            assert r.status_code == 200, r.text
            assert r.json()["state"] in ("queued", "running")
    finally:
        pool = getattr(fast.state, "worker_pool", None)
        if pool is not None:
            await pool.stop()
        scheduler = getattr(fast.state, "scheduler", None)
        if scheduler is not None:
            await scheduler.stop()
        http = getattr(fast.state, "http", None)
        if http is not None:
            await http.aclose()
        await engine.dispose()

"""后台管理 REST 测试（DESIGN §14）：projects / models CRUD + Fernet 脱敏 + 连通测试。

离线：临时 SQLite + httpx ASGI；JWT 由 auth.issue_token 签发；探测函数 monkeypatch 掉即零网络。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.admin import forges, models as admin_models
from codereview_ai.api.admin import notifiers, projects, reviews, tasks
from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ForgeConfig, ModelConfig, ReviewFinding, ReviewTask


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}"
    engine = create_engine(url)
    await init_db(engine)

    settings = type("S", (), {"secret_key": "s", "encryption_key": _fernet_key()})()

    fast = FastAPI()
    fast.state.engine = engine
    fast.state.settings = settings
    fast.state.config_repository = ConfigRepository(engine, encryption_key=_fernet_key())
    fast.state.forge_registry = None  # 单测不启动 worker；热更分支被跳过
    fast.include_router(auth_router, prefix="/api")
    fast.include_router(projects.router, prefix="/api")
    fast.include_router(admin_models.router, prefix="/api")
    fast.include_router(notifiers.router, prefix="/api")
    fast.include_router(forges.router, prefix="/api")
    fast.include_router(reviews.router, prefix="/api")
    fast.include_router(tasks.router, prefix="/api")

    token = issue_token(settings.secret_key)
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _client(fast: FastAPI, token: str) -> TestClient:
    return TestClient(fast, headers={"Authorization": f"Bearer {token}"})


def test_projects_crud_roundtrip(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.post("/api/projects", json={
            "provider": "gitlab", "repo_id": "123", "repo_full_name": "a/b",
            "branch_rule": "main", "review_strategy": "diff", "score_threshold": 90,
        })
        assert r.status_code == 201
        pid = r.json()["id"]
        assert r.json()["score_threshold"] == 90

        # 无 token → 401
        assert TestClient(fast).get("/api/projects").status_code == 401

        lst = c.get("/api/projects").json()
        assert len(lst) == 1 and lst[0]["repo_full_name"] == "a/b"

        upd = c.put(f"/api/projects/{pid}", json={
            "provider": "gitlab", "repo_id": "123", "branch_rule": "dev",
            "score_threshold": 60,
            "push_enabled": True, "push_branch_globs": "main,release/*",
        }).json()
        assert upd["branch_rule"] == "dev" and upd["score_threshold"] == 60
        assert upd["push_enabled"] is True and upd["push_branch_globs"] == "main,release/*"

        # push_enabled 可回写为 null → 继承全局默认
        upd2 = c.put(f"/api/projects/{pid}", json={
            "provider": "gitlab", "repo_id": "123", "push_enabled": None,
        }).json()
        assert upd2["push_enabled"] is None

        assert c.get("/api/projects/9999").status_code == 404
        assert c.delete(f"/api/projects/{pid}").status_code == 204
        assert c.get("/api/projects").json() == []


def test_models_create_masks_and_reads_encrypted(app):
    fast, token = app
    fast.state.settings.encryption_key = _fernet_key()
    with _client(fast, token) as c:
        r = c.post("/api/models", json={
            "name": "deepseek", "provider": "deepseek", "model": "deepseek-chat",
            "api_key": "sk-secret-abc", "base_url": "https://x", "priority": 5,
        })
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        # 读路径统一回显 ******
        assert r.json()["api_key"] == "******"

        # 落库是密文、明文不出现
        found: list[ModelConfig] = []

        async def _fetch() -> None:
            from sqlalchemy import select

            session = session_factory(fast.state.engine)
            async with session() as s:
                found.append((await s.execute(select(ModelConfig))).scalar_one())

        import asyncio

        asyncio.get_event_loop().run_until_complete(_fetch())
        row = found[0]
        assert row.api_key_encrypted != "sk-secret-abc"
        assert "sk-secret-abc" not in row.api_key_encrypted
        from codereview_ai.crypto import decrypt

        assert decrypt(row.api_key_encrypted, _fernet_key()) == "sk-secret-abc"

        # 列表与详情回显掩码
        assert c.get("/api/models").json()[0]["api_key"] == "******"
        assert c.get(f"/api/models/{mid}").json()["api_key"] == "******"

        # 更新：掩码表示保留原文；明文则重加密
        upd = c.put(f"/api/models/{mid}", json={
            "name": "deepseek", "model": "deepseek-chat", "api_key": "******", "priority": 9,
        }).json()
        assert upd["priority"] == 9 and upd["api_key"] == "******"
        upd2 = c.put(f"/api/models/{mid}", json={
            "name": "deepseek", "model": "deepseek-chat", "api_key": "sk-new-xyz",
        }).json()
        assert upd2["api_key"] == "******"


def test_models_probe_with_injected_probe(app, monkeypatch):
    fast, token = app
    with _client(fast, token) as c:
        c.post("/api/models", json={"name": "m", "provider": "deepseek",
                                    "model": "deepseek-chat", "api_key": "sk-x"})
        mid = c.get("/api/models").json()[0]["id"]

        async def fake_probe(cfg, prompt, **kw) -> bool:
            assert kw["encryption_key"]
            return True

        monkeypatch.setattr(admin_models, "probe_model", fake_probe)
        assert c.post(f"/api/models/{mid}/test", json={"prompt": "hi"}).status_code == 200

        async def fail_probe(cfg, prompt, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(admin_models, "probe_model", fail_probe)
        assert c.post(f"/api/models/{mid}/test", json={}).status_code == 502


def test_models_missing_api_key_400_on_create(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.post("/api/models", json={
            "name": "bad", "provider": "x", "model": "x", "api_key": "******",
        })
        assert r.status_code == 400


def test_notifiers_crud_and_masking(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.post("/api/notifiers", json={
            "channel": "dingtalk", "webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "secret": "SECsecret", "project_id": None, "at_threshold": 60,
        })
        assert r.status_code == 201, r.text
        nid = r.json()["id"]
        assert r.json()["webhook"] == "******" and r.json()["secret"] == "******"
        assert r.json()["channel"] == "dingtalk" and r.json()["project_id"] is None

        upd = c.put(f"/api/notifiers/{nid}", json={
            "channel": "dingtalk", "webhook": "******", "secret": "******",
            "project_id": 3, "at_threshold": 80,
        }).json()
        assert upd["project_id"] == 3 and upd["at_threshold"] == 80

        assert c.get("/api/notifiers").json()[0]["webhook"] == "******"
        assert c.delete(f"/api/notifiers/{nid}").status_code == 204
        assert c.delete(f"/api/notifiers/{nid}").status_code == 404


def test_reviews_list_pagination_and_detail(app):
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            t1 = ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                            branch="main", head_sha="aaa", state="completed", score_total=80,
                            summary_md="# ok")
            t2 = ReviewTask(provider="gitlab", repo_id="1", pr_number=2, event_type="mr",
                            branch="main", head_sha="bbb", state="failed", error="boom")
            s.add_all([t1, t2])
            await s.flush()
            s.add(ReviewFinding(task_id=t1.id, fingerprint="h", severity="high",
                                category="bug", file="a.py", title="t"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    with _client(fast, token) as c:
        page = c.get("/api/reviews?state=completed&limit=10").json()
        assert page["total"] == 1 and len(page["items"]) == 1
        assert page["items"][0]["score_total"] == 80

        all_page = c.get("/api/reviews?limit=1").json()
        assert all_page["total"] == 2 and len(all_page["items"]) == 1

        detail = c.get("/api/reviews/1").json()
        assert detail["state"] == "completed"
        assert len(detail["findings"]) == 1 and detail["findings"][0]["severity"] == "high"

        assert c.get("/api/reviews/999").status_code == 404


def test_tasks_list_and_retry(app):
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=9, event_type="mr",
                             branch="main", head_sha="abc", state="failed", attempt=1,
                             error="timeout"))
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=8, event_type="mr",
                             branch="main", head_sha="def", state="completed"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    with _client(fast, token) as c:
        failed = c.get("/api/tasks?state=failed").json()
        assert len(failed) == 1 and failed[0]["attempt"] == 1

        tid = failed[0]["id"]
        retried = c.post(f"/api/tasks/{tid}/retry").json()
        assert retried["state"] == "queued" and retried["attempt"] == 2

        # 已完成任务不能重试 → 409
        done_id = c.get("/api/tasks?state=completed").json()[0]["id"]
        assert c.post(f"/api/tasks/{done_id}/retry").status_code == 409
        assert c.post("/api/tasks/9999/retry").status_code == 404


def test_retry_reenqueues_payload(app):
    """重试失败任务时把原始 webhook 重新投进队列（修复原先一直排队中的 bug）。"""
    fast, token = app
    payload = '{"action": "open", "object_attributes": {"action": "open"}}'
    payload_bytes = payload.encode("utf-8")

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=7, event_type="mr",
                             branch="main", head_sha="xyz", state="failed", attempt=1,
                             error="boom", payload=payload))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    calls: list[tuple[str, bytes]] = []

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            calls.append((provider, raw))
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()

    with _client(fast, token) as c:
        failed = c.get("/api/tasks?state=failed").json()
        tid = failed[0]["id"]
        retried = c.post(f"/api/tasks/{tid}/retry").json()
        assert retried["state"] == "queued" and retried["attempt"] == 2
        assert calls == [("gitlab", payload_bytes)]


def test_tasks_retry_skipped_recoverable_only(app):
    """门控/配置类 skipped 可补审；删分支 skipped 有 head 可审，禁止重试（409）。"""
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p1", state="skipped",
                             skip_reason="push_disabled", error="push 审查未开启...",
                             payload='{"x":1}'))
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p2", state="skipped",
                             skip_reason="branch_deleted", error="push 事件为删除分支..."))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()

    with _client(fast, token) as c:
        lst = c.get("/api/tasks").json()
        disabled = next(t for t in lst if t["skip_reason"] == "push_disabled")
        deleted = next(t for t in lst if t["skip_reason"] == "branch_deleted")
        # skip_reason 已在列表响应露出，前端据此亮「补审」按钮
        assert disabled["skip_reason"] == "push_disabled"

        r = c.post(f"/api/tasks/{disabled['id']}/retry")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "queued"
        assert c.post(f"/api/tasks/{deleted['id']}/retry").status_code == 409
        # mr 轨 skipped（不应出现，防御态）也不可重试
        async def _mr() -> None:
            session = session_factory(fast.state.engine)
            async with session() as s:
                s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=5, event_type="mr",
                                 branch="main", head_sha="m1", state="skipped",
                                 skip_reason="branch_mismatch"))
                await s.commit()
        asyncio.get_event_loop().run_until_complete(_mr())
        m = next(t for t in c.get("/api/tasks").json() if t["event_type"] == "mr")
        assert c.post(f"/api/tasks/{m['id']}/retry").status_code == 409


def test_retry_push_failed_sets_force_rerun(app):
    """push 轨失败任务重试后置 force_rerun，令 worker 绕过幂等预检真正重跑（修空转 bug）。"""
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p3", state="failed", attempt=1,
                             error="boom", payload='{"x":1}'))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()
    tid = None
    async def _get() -> None:
        nonlocal tid
        from sqlalchemy import select
        session = session_factory(fast.state.engine)
        async with session() as s:
            tid = (await s.execute(
                select(ReviewTask).where(ReviewTask.event_type == "push"))).scalar_one().id
    asyncio.get_event_loop().run_until_complete(_get())

    with _client(fast, token) as c:
        c.post(f"/api/tasks/{tid}/retry").json()

    async def _assert() -> None:
        from sqlalchemy import select
        session = session_factory(fast.state.engine)
        async with session() as s:
            row = (await s.execute(select(ReviewTask))).scalar_one()
            assert row.force_rerun is True  # push 重试写到 force_rerun，供 worker 绕过幂等
    asyncio.get_event_loop().run_until_complete(_assert())


def test_forges_list_synthesizes_defaults(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CR_GITLAB_TOKEN", raising=False)
    with _client(fast, token) as c:
        rows = c.get("/api/forges").json()
        assert {r["provider"] for r in rows} == {"github", "gitlab"}
        # 未配置：URL 回退默认、token 空、env_active False
        gh = next(r for r in rows if r["provider"] == "github")
        assert gh["url"] == "https://api.github.com" and gh["token"] == ""
        assert gh["env_active"] is False


def test_forges_upsert_encrypts_and_masks(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    with _client(fast, token) as c:
        r = c.put("/api/forges/github", json={"url": "https://gh.example", "token": "gh-secret-abc", "enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["token"] == "******"
        assert r.json()["url"] == "https://gh.example"

        # 落库密文，明文不出现
        found: list[ForgeConfig] = []

        async def _fetch() -> None:
            from sqlalchemy import select

            session = session_factory(fast.state.engine)
            async with session() as s:
                found.append((await s.execute(select(ForgeConfig))).scalar_one())

        import asyncio

        asyncio.get_event_loop().run_until_complete(_fetch())
        row = found[0]
        assert row.token_encrypted not in ("", "gh-secret-abc")
        from codereview_ai.crypto import decrypt

        assert decrypt(row.token_encrypted, _fernet_key()) == "gh-secret-abc"

        # 掩码提交 → 保留原密文；新明文 → 重加密
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "******", "enabled": True})
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "gh-new-xyz", "enabled": True})

        async def _last() -> None:
            nonlocal row
            from sqlalchemy import select

            async with session_factory(fast.state.engine)() as s:
                row = (await s.execute(select(ForgeConfig))).scalar_one()

        asyncio.get_event_loop().run_until_complete(_last())
        assert decrypt(row.token_encrypted, _fernet_key()) == "gh-new-xyz"


def test_forges_probe_zero_network_via_injected_probe(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    with _client(fast, token) as c:
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "tok", "enabled": True})

        captured: dict = {}

        async def fake_probe(provider, url, token):
            captured.update(provider=provider, url=url, token=token)
            return True

        monkeypatch.setattr(forges, "probe_forge", fake_probe)
        r = c.post("/api/forges/github/test", json={})
        assert r.status_code == 200, r.text
        assert captured == {"provider": "github", "url": "https://gh.example", "token": "tok"}

        async def fail_probe(provider, url, token):
            raise RuntimeError("boom")

        monkeypatch.setattr(forges, "probe_forge", fail_probe)
        assert c.post("/api/forges/github/test", json={}).status_code == 502


def test_forges_probe_invalid_provider_404(app):
    fast, token = app
    with _client(fast, token) as c:
        assert c.put("/api/forges/gitee", json={"url": "u", "token": "t"}).status_code == 404
        assert c.post("/api/forges/gitee/test", json={}).status_code == 404

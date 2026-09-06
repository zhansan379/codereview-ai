"""后台管理 REST 测试（DESIGN §14）：projects / models CRUD + Fernet 脱敏 + 连通测试。

离线：临时 SQLite + httpx ASGI；JWT 由 auth.issue_token 签发；探测函数 monkeypatch 掉即零网络。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.admin import models as admin_models
from codereview_ai.api.admin import projects as admin_projects
from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ModelConfig


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
    fast.include_router(auth_router, prefix="/api")
    fast.include_router(admin_projects.router, prefix="/api")
    fast.include_router(admin_models.router, prefix="/api")

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
        }).json()
        assert upd["branch_rule"] == "dev" and upd["score_threshold"] == 60

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

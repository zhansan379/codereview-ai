"""BYOK 租户自备密钥测试：workspace 级 LLM key + forge PAT 的下沉与解析。

离线：临时 SQLite + TestClient（API 侧）/ ConfigRepository + ForgeRegistry（解析侧）。
覆盖：
- 租户在自有 workspace 建模型/forge（Fernet 落库，读回 `******`）；
- 非 owner 读/改别的空间 → 403；平台豁免仅超管可设（member → 403）；
- 解析：租户自带 key → 用其配置；无自带 + 无豁免 → None（降级）；无 + 豁免 → 回落全局；
- 注册表：按 (provider, repo_id) 为各租户取到各自 PAT 的适配器。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.api.admin.projects import router as projects_router
from codereview_ai.api.tenant.secrets import router as tenant_secrets_router
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.crypto import encrypt
from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import (
    Project,
    Role,
    User,
    Workspace,
    WorkspaceForgeConfig,
    WorkspaceModelConfig,
)
from codereview_ai.storage.seed import ensure_member_role, ensure_workspace_backfill, seed_rbac
from tests.unit.helpers import make_admin_app


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


async def _extra_seed(session) -> None:
    await ensure_member_role(session)
    await ensure_workspace_backfill(session)


@pytest.fixture
async def api_app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    """挂好租户 secrets 路由的 app + admin token（邮箱验证跳过）。"""
    fast, token, _admin, engine = await make_admin_app(
        tmp_path, db_name="byok_api.db",
        routers=[tenant_secrets_router, projects_router], seed=_extra_seed,
    )
    fast.state.config_repository = None
    fast.state.forge_registry = None
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _reg(c: TestClient, username: str, password: str = "pass1234") -> dict:
    r = c.post("/api/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _login(c: TestClient, username: str, password: str = "pass1234") -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------- API：模型 CRUD + mask ----------------

def test_tenant_creates_and_lists_workspace_models(api_app):
    fast, _ = api_app
    with TestClient(fast) as c:
        reg = _reg(c, "alice")
        ws = reg["workspace"]["id"]
        h = _hdr(_login(c, "alice"))
        # 建模型（密钥加密落库）
        r = c.post(f"/api/workspaces/{ws}/models", json={
            "name": "ws-llm", "provider": "openai", "model": "openai/gpt-4o-mini",
            "api_key": "sk-ws-secret", "base_url": "https://api.moonshot.cn/v1",
            "temperature": 0.7, "max_tokens": 4096, "priority": 5, "enabled": True,
        }, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["api_key"] == "******"  # 读回 mask
        # 列表也只回显 mask
        items = c.get(f"/api/workspaces/{ws}/models", headers=h).json()
        assert len(items) == 1
        assert items[0]["name"] == "ws-llm"
        assert items[0]["api_key"] == "******"
        mid = items[0]["id"]
        # 更新：api_key 传 ****** 保留原密文
        r2 = c.put(f"/api/workspaces/{ws}/models/{mid}", json={
            "name": "ws-llm2", "provider": "openai", "model": "openai/gpt-4o-mini",
            "api_key": "******", "priority": 9, "enabled": True,
        }, headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["name"] == "ws-llm2"
        assert r2.json()["priority"] == 9
        # 删除
        assert c.delete(f"/api/workspaces/{ws}/models/{mid}", headers=h).status_code == 204
        assert c.get(f"/api/workspaces/{ws}/models", headers=h).json() == []


def test_workspace_forge_upsert_clears(api_app):
    fast, _ = api_app
    with TestClient(fast) as c:
        ws = _reg(c, "bob")["workspace"]["id"]
        h = _hdr(_login(c, "bob"))
        # 未配置 → 默认 url + 空 token
        got = c.get(f"/api/workspaces/{ws}/forges/gitlab", headers=h).json()
        assert got["token"] == ""
        assert got["url"] == ""  # 未配置 → 空（默认 url 由解析侧补）
        # 保存 PAT
        put = c.put(f"/api/workspaces/{ws}/forges/gitlab", json={
            "url": "", "token": "glpat-ws", "enabled": True,
        }, headers=h)
        assert put.status_code == 200, put.text
        assert put.json()["token"] == "******"
        # 清除 → 回到空
        assert c.delete(f"/api/workspaces/{ws}/forges/gitlab", headers=h).status_code == 204
        assert c.get(f"/api/workspaces/{ws}/forges/gitlab", headers=h).json()["token"] == ""


def test_non_owner_cannot_touch_other_workspace(api_app):
    fast, _ = api_app
    with TestClient(fast) as c:
        a = _reg(c, "alice")["workspace"]["id"]
        _reg(c, "bob")
        hB = _hdr(_login(c, "bob"))
        # bob 读/写 alice 的空间 → 403
        assert c.get(f"/api/workspaces/{a}/models", headers=hB).status_code == 403
        assert c.post(f"/api/workspaces/{a}/models", json={
            "name": "x", "provider": "openai", "api_key": "k",
        }, headers=hB).status_code == 403
        assert c.put(f"/api/workspaces/{a}/forges/github", json={"token": "t"},
                     headers=hB).status_code == 403


def test_platform_fallback_only_super(api_app):
    fast, admin_token = api_app
    with TestClient(fast) as c:
        ws = _reg(c, "carol")["workspace"]["id"]
        h = _hdr(_login(c, "carol"))
        # member 设豁免 → 403
        assert c.put(f"/api/workspaces/{ws}/platform-fallback", json={"enabled": True},
                     headers=h).status_code == 403
        # 超管设豁免 → 200 且读回
        r = c.put(f"/api/workspaces/{ws}/platform-fallback", json={"enabled": True},
                  headers=_hdr(admin_token))
        assert r.status_code == 200, r.text
        assert r.json()["platform_fallback"] is True
        assert c.get(f"/api/workspaces/{ws}/platform-fallback", headers=_hdr(admin_token)
                     ).json()["platform_fallback"] is True
        # 超管关闭豁免
        c.put(f"/api/workspaces/{ws}/platform-fallback", json={"enabled": False},
              headers=_hdr(admin_token))
        assert c.get(f"/api/workspaces/{ws}/platform-fallback", headers=h
                     ).json()["platform_fallback"] is False


# ---------------- 解析：workspace reviewer / forge ----------------

@pytest.fixture
async def repo_engine(tmp_path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'byok_repo.db'}")
    await init_db(engine)
    async with session_factory(engine)() as s:
        await seed_rbac(s, "hunter2")
        await s.commit()
    yield engine
    await engine.dispose()


async def _make_tenant(engine: AsyncEngine, username: str) -> tuple[int, int]:
    """建一个非超管用户 + 其私有 workspace（member/developer 角色即可）。"""
    async with session_factory(engine)() as s:
        role = (await s.execute(select(Role).where(Role.name == "开发"))).scalar_one()
        u = User(username=username, password_hash="x", role_id=role.id, enabled=True)
        s.add(u)
        await s.flush()
        w = Workspace(name=username, slug=username + "w", owner_id=u.id)
        s.add(w)
        await s.flush()
        await s.commit()
        return u.id, w.id


async def _seed_ws_model(engine, ws_id: int, *, name="ws-m", model="openai/ws-model",
                         api_key="ws-key", priority=1, enabled=True) -> None:
    async with session_factory(engine)() as s:
        s.add(WorkspaceModelConfig(
            workspace_id=ws_id, name=name, provider="openai", model=model,
            api_key_encrypted=encrypt(api_key, _fernet_key()),
            base_url="", temperature=0.7, max_tokens=4096, priority=priority,
            capabilities=[], enabled=enabled,
        ))
        await s.commit()


async def _seed_global_model(engine, *, model="openai/global", api_key="global-key") -> None:
    async with session_factory(engine)() as s:
        from codereview_ai.storage.models import ModelConfig
        s.add(ModelConfig(
            name="global", provider="openai", model=model,
            api_key_encrypted=encrypt(api_key, _fernet_key()),
            priority=1, enabled=True, base_url="",
        ))
        await s.commit()


async def _seed_ws_forge(engine, ws_id: int, provider: str, token: str) -> None:
    async with session_factory(engine)() as s:
        s.add(WorkspaceForgeConfig(
            workspace_id=ws_id, provider=provider, url="",
            token_encrypted=encrypt(token, _fernet_key()), enabled=True,
        ))
        await s.commit()


async def _seed_repo(engine, ws_id: int, provider: str, repo_id: str, enabled=True) -> int:
    async with session_factory(engine)() as s:
        p = Project(provider=provider, repo_id=repo_id, workspace_id=ws_id,
                    enabled=enabled, repo_full_name=f"{provider}/{repo_id}")
        s.add(p)
        await s.commit()
        return p.id


async def test_workspace_reviewer_uses_own_chain(repo_engine):
    _, ws_id = await _make_tenant(repo_engine, "alice")
    await _seed_ws_model(repo_engine, ws_id, api_key="ws-key-1")

    repo = ConfigRepository(repo_engine, encryption_key=_fernet_key())
    reviewer = await repo.build_workspace_reviewer(ws_id)
    assert isinstance(reviewer, Reviewer)
    # 链首使用该 workspace 自带模型（不是全局）
    assert reviewer.gateway.model == "openai/ws-model"


async def test_workspace_no_key_no_fallback_degrades_none(repo_engine):
    _, ws_id = await _make_tenant(repo_engine, "bob")
    repo = ConfigRepository(repo_engine, encryption_key=_fernet_key())
    assert await repo.build_workspace_reviewer(ws_id) is None  # 必须自带 key → 降级


async def test_workspace_no_key_fallback_uses_global(repo_engine):
    _, ws_id = await _make_tenant(repo_engine, "carol")
    await _seed_global_model(repo_engine, model="openai/global")
    async with session_factory(repo_engine)() as s:
        w = (await s.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        w.platform_fallback = True
        await s.commit()

    repo = ConfigRepository(repo_engine, encryption_key=_fernet_key())
    reviewer = await repo.build_workspace_reviewer(ws_id)
    assert isinstance(reviewer, Reviewer)
    assert reviewer.gateway.model == "openai/global"  # 豁免 → 回落全局


async def test_resolve_forge_for_repo_per_workspace(repo_engine):
    """两个租户各自 PAT → 解析各自 token；无自带 + 无豁免的租户 → None。"""
    _, wsA = await _make_tenant(repo_engine, "alice")
    _, wsB = await _make_tenant(repo_engine, "bob")
    await _seed_ws_forge(repo_engine, wsA, "github", "pat-A")
    await _seed_ws_forge(repo_engine, wsB, "github", "pat-B")
    await _seed_repo(repo_engine, wsA, "github", "100")
    await _seed_repo(repo_engine, wsB, "github", "200")

    repo = ConfigRepository(repo_engine, encryption_key=_fernet_key())
    fa, fb = (
        await repo.resolve_forge_for_repo("github", "100"),
        await repo.resolve_forge_for_repo("github", "200"),
    )
    assert fa is not None and fb is not None
    assert fa.token == "pat-A"
    assert fb.token == "pat-B"
    # 未注册仓库 → None
    assert await repo.resolve_forge_for_repo("github", "999") is None


async def test_forge_registry_get_for_repo_adapters(repo_engine):
    """ForgeRegistry.get_for_repo：不同租户拿到挂各自 PAT 的 GitHub 适配器，并缓存。"""
    _, wsA = await _make_tenant(repo_engine, "alice")
    _, wsB = await _make_tenant(repo_engine, "bob")
    await _seed_ws_forge(repo_engine, wsA, "github", "pat-A")
    await _seed_ws_forge(repo_engine, wsB, "github", "pat-B")
    await _seed_repo(repo_engine, wsA, "github", "100")
    await _seed_repo(repo_engine, wsB, "github", "200")

    dummy = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    repo = ConfigRepository(repo_engine, encryption_key=_fernet_key())
    reg = ForgeRegistry(repo, dummy)

    fa = await reg.get_for_repo("github", "100")
    fb = await reg.get_for_repo("github", "200")
    assert isinstance(fa, GitHubForge) and isinstance(fb, GitHubForge)
    assert fa._token == "pat-A"
    assert fb._token == "pat-B"
    # 无自带凭据且未豁免 → None（跳过，不注册）
    assert await reg.get_for_repo("github", "999") is None
    await dummy.aclose()

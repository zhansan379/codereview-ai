"""多租户开放注册（阶段 A 后端地基）测试：注册闭环 + 私有 workspace + 租户隔离 + 回填。

离线：临时 SQLite + httpx ASGI/TestClient。覆盖：
- 注册建 member 角色用户 + 私有 workspace（owner=本人），随后可登录；
- 用户名重复 409、弱密码 400；
- 注册用户能建项目且归属自己的 workspace；
- 两个租户互相隔离（A 看不见 B 的项目）；
- 存量回填幂等（默认工作区只建一次、存量项目归入其下）。

注意 Windows 子进程解码：仓库既有约定显式 encoding="utf-8"（见 memory）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from codereview_ai.api.admin import projects as projects_mod
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import Project, Workspace
from codereview_ai.storage.seed import (
    ensure_member_role,
    ensure_workspace_backfill,
    seed_rbac,
)
from tests.unit.helpers import make_admin_app


async def _extra_seed(session) -> None:
    """seed_rbac 之后补齐 member 角色与默认工作区（模拟主启动时序）。"""
    await ensure_member_role(session)
    await ensure_workspace_backfill(session)


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    fast, token, _admin, engine = await make_admin_app(
        tmp_path, db_name="ws.db", routers=[projects_mod.router], seed=_extra_seed,
    )
    fast.state.config_repository = None
    fast.state.forge_registry = None
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _reg(c: TestClient, username: str, password: str = "pass1234", display_name: str = "") -> dict:
    body = {"username": username, "password": password}
    if display_name:
        body["display_name"] = display_name
    r = c.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_register_creates_private_workspace_and_can_login(app):
    fast, _ = app
    with TestClient(fast) as c:
        body = _reg(c, "alice", display_name="Alice")
        assert body["user"]["username"] == "alice"
        assert body["user"]["role_name"] == "成员"  # 自助注册默认角色（仅项目作用域，非超管）
        assert body["workspace"]["slug"] == "alice"
        assert body["workspace"]["name"] == "Alice"
        # 随后可登录
        token = _login(c, "alice", "pass1234")
        me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["user"]["role_name"] == "成员"


def test_register_duplicate_username_409(app):
    fast, _ = app
    with TestClient(fast) as c:
        _reg(c, "bob")
        r = c.post("/api/auth/register", json={"username": "bob", "password": "pass1234"})
        assert r.status_code == 409
        assert "已存在" in r.json()["detail"]


def test_register_weak_password_400(app):
    fast, _ = app
    with TestClient(fast) as c:
        r = c.post("/api/auth/register", json={"username": "carol", "password": "short"})
        assert r.status_code == 400
        assert "至少" in r.json()["detail"]


def test_registered_user_creates_project_in_own_workspace(app):
    fast, _ = app
    with TestClient(fast) as c:
        reg = _reg(c, "dave")
        ws_id = reg["workspace"]["id"]
        h = {"Authorization": f"Bearer {_login(c, 'dave', 'pass1234')}"}
        r = c.post("/api/projects", json={
            "provider": "gitlab", "repo_id": "100", "repo_full_name": "dave/app",
        }, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["workspace_id"] == ws_id  # 自动归属自己的空间


def test_tenants_are_isolated(app):
    fast, _ = app
    with TestClient(fast) as c:
        _reg(c, "erin")
        _reg(c, "frank")
        h_erin = {"Authorization": f"Bearer {_login(c, 'erin', 'pass1234')}"}
        h_frank = {"Authorization": f"Bearer {_login(c, 'frank', 'pass1234')}"}
        r = c.post("/api/projects", json={"provider": "gitlab", "repo_id": "200"}, headers=h_erin)
        assert r.status_code == 201, r.text
        # frank 看不到 erin 的项目，erin 看得到自己的
        assert c.get("/api/projects", headers=h_frank).json() == []
        assert len(c.get("/api/projects", headers=h_erin).json()) == 1


async def test_backfill_idempotent_and_assigns_existing_projects(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    await init_db(engine)
    async with session_factory(engine)() as s:
        await seed_rbac(s, "hunter2")
        # 存量库：回填前已存在无归属项目
        s.add(Project(provider="gitlab", repo_id="500"))
        await s.commit()
    async with session_factory(engine)() as s:
        created = await ensure_workspace_backfill(s)
        assert created is True
        ws = (await s.execute(select(Workspace))).scalar_one()
        assert ws.owner_id is not None  # 默认工作区归属首个用户（admin）
        p = (await s.execute(select(Project))).scalar_one()
        assert p.workspace_id == ws.id  # 存量项目归入默认工作区
        # 幂等：第二遍不再建空间
        assert await ensure_workspace_backfill(s) is False
        count = (await s.execute(select(func.count()).select_from(Workspace))).scalar_one()
        assert count == 1
    await engine.dispose()

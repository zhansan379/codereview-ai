"""用户/角色管理 REST（F5.11 users.py / roles.py）：CRUD、密码策略、保护规则。

admin 超管驱动；role id 经 `GET /api/roles`（含 builtin_code）取内置角色。
离线临时 SQLite。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from codereview_ai.api.admin import roles as roles_router
from codereview_ai.api.admin import users as users_router
from tests.unit.helpers import make_admin_app


@pytest.fixture
def ctx(tmp_path):
    fast, token, _admin, engine = asyncio.run(make_admin_app(
        tmp_path, db_name="ur.db", routers=[users_router.router, roles_router.router]))
    holder = type("C", (), {"fast": fast, "token": token, "engine": engine})()
    yield holder
    asyncio.run(engine.dispose())


def _c(ctx) -> Iterator:
    return TestClient(ctx.fast, headers={"Authorization": f"Bearer {ctx.token}"})


def _role_id(ctx, builtin: str) -> int:
    rows = _c(ctx).get("/api/roles").json()
    return next(r["id"] for r in rows if r["builtin_code"] == builtin)


# ── 用户管理 ──────────────────────────────────────────────────────────────

def test_create_update_reset_password(ctx):
    c = _c(ctx)
    dev_id = _role_id(ctx, "developer")
    r = c.post("/api/users", json={
        "username": "bob", "password": "secret123", "display_name": "Bob", "role_id": dev_id})
    assert r.status_code == 201
    uid = r.json()["id"]
    assert r.json()["role_name"] == "开发"

    r = c.put(f"/api/users/{uid}", json={"display_name": "Bobby"})
    assert r.status_code == 200 and r.json()["display_name"] == "Bobby"

    r = c.post(f"/api/users/{uid}/reset-password", json={"password": "newpass88"})
    assert r.status_code == 200

    # 新密码可登录（独立引擎验证密码哈希被正确更新）
    async def _verify():
        from sqlalchemy import select

        from codereview_ai.security import verify_password
        from codereview_ai.storage.db import session_factory
        from codereview_ai.storage.models import User
        ss = session_factory(ctx.engine)
        async with ss() as s:
            u = (await s.execute(select(User).where(User.username == "bob"))).scalar_one()
            return verify_password("newpass88", u.password_hash)
    assert asyncio.run(_verify())


def test_create_duplicate_and_weak_password(ctx):
    c = _c(ctx)
    dev_id = _role_id(ctx, "developer")
    body = {"username": "bob", "password": "secret123", "role_id": dev_id}
    assert c.post("/api/users", json=body).status_code == 201
    assert c.post("/api/users", json=body).status_code == 409  # 重名
    assert c.post("/api/users", json={**body, "username": "bob2", "password": "short"}).status_code == 400  # noqa: E501


def test_cannot_delete_self_or_last_admin(ctx):
    c = _c(ctx)
    admin_id = next(u["id"] for u in c.get("/api/users").json() if u["username"] == "admin")
    assert c.delete(f"/api/users/{admin_id}").status_code == 400  # 删最后一个超管
    # 禁用最后一个启用超管 → 400
    # 先把 admin 属性通过 put enabled 试（应 400 拦）
    assert c.put(f"/api/users/{admin_id}", json={"enabled": False}).status_code == 400


def test_user_list_and_disable(ctx):
    c = _c(ctx)
    dev_id = _role_id(ctx, "developer")
    uid = c.post("/api/users", json={
        "username": "carol", "password": "secret123", "role_id": dev_id}).json()["id"]
    assert c.put(f"/api/users/{uid}", json={"enabled": False}).status_code == 200
    usernames = {u["username"]: u["enabled"] for u in c.get("/api/users").json()}
    assert usernames["carol"] is False


# ── 角色管理 ──────────────────────────────────────────────────────────────

def test_role_permissions_catalog(ctx):
    perms = {p["code"] for p in _c(ctx).get("/api/roles/permissions").json()}
    assert "projects:view" in perms and "users:manage" in perms


def test_create_custom_role_and_assign_permissions(ctx):
    c = _c(ctx)
    r = c.post("/api/roles", json={
        "name": "QA", "all_projects": True,
        "permission_codes": ["projects:view", "reviews:view"]})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["is_super"] is False
    assert set(r.json()["permissions"]) == {"projects:view", "reviews:view"}

    r = c.put(f"/api/roles/{rid}/permissions",
              json={"permission_codes": ["reviews:manage"]})
    assert set(r.json()["permissions"]) == {"reviews:manage"}

    # 建个引用它的用户后再删 → 409
    assert c.post("/api/users", json={
        "username": "qa1", "password": "secret123", "role_id": rid}).status_code == 201
    assert c.delete(f"/api/roles/{rid}").status_code == 409


def test_system_role_protected(ctx):
    c = _c(ctx)
    admin_role = next(r for r in c.get("/api/roles").json() if r["builtin_code"] == "admin")
    assert c.delete(f"/api/roles/{admin_role['id']}").status_code == 403
    assert c.put(f"/api/roles/{admin_role['id']}", json={"all_projects": True}).status_code == 403


def test_role_member_count(ctx):
    c = _c(ctx)
    admin_role = next(r for r in c.get("/api/roles").json() if r["builtin_code"] == "admin")
    assert admin_role["member_count"] >= 1  # 至少有 seed 的 admin

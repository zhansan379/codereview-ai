"""后台鉴权测试（DESIGN §14.3，F5.11）：多用户登录 + JWT(sub=user id) + get_current_user。

离线：临时 SQLite + 真实 auth 路由 + seed 出的 admin（超管）；预置 dev1(developer)、
dev2(禁用)。fixture 走代码库惯例（async 构建 + TestClient 同步调用）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai import security
from codereview_ai.api import deps
from codereview_ai.api.auth import issue_token
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import Role, User

SECRET = "s"


async def _add_user(s, username, password, builtin, enabled=True):
    role = (await s.execute(select(Role).where(Role.builtin_code == builtin))).scalar_one()
    u = User(username=username, password_hash=security.hash_password(password),
             display_name=username, enabled=enabled, role_id=role.id)
    s.add(u)
    await s.flush()
    return u.id


@pytest.fixture
def auth_app(tmp_path):
    """构建已 seed 的 app；含 dev1(developer)、dev2(禁用)。

    因 make_admin_app 是 async，暂用异步构建 + 缓存；测试内 TestClient 同步调用。
    """

    class Holder:
        pass

    from tests.unit.helpers import make_admin_app as _ma

    async def _build():
        async def seed(s):
            await _add_user(s, "dev1", "devpass1", "developer")
            await _add_user(s, "dev2", "devpass2", "developer", enabled=False)

        fast, token, admin, engine = await _ma(tmp_path, db_name="auth.db", seed=seed)

        @fast.get("/api/secure")
        async def secure(user: User = Depends(deps.get_current_user)):
            return {"user": user.username, "id": user.id}

        h = Holder()
        h.fast, h.token, h.admin, h.engine = fast, token, admin, engine
        return h

    import asyncio
    holder = asyncio.run(_build())
    yield holder
    asyncio.run(holder.engine.dispose())


def _client(fast, token: str | None = None) -> Iterator:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(fast, headers=headers)


def _dev1_id(h) -> int:
    async def _q():
        s = session_factory(h.engine)
        async with s() as sess:
            return (await sess.execute(
                select(User).where(User.username == "dev1"))).scalar_one().id
    import asyncio
    return asyncio.run(_q())


def test_login_success_issues_jwt_with_user_id(auth_app):
    h = auth_app
    body = _client(h.fast).post(
        "/api/auth/login", json={"username": "admin", "password": "hunter2"}
    ).json()
    payload = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"])
    assert payload["sub"] == str(h.admin.id)  # sub=用户 id
    assert body["user"]["username"] == "admin"
    assert "users:manage" in body["permissions"]  # 超管全量


def test_login_wrong_password_or_unknown_user_401(auth_app):
    c = _client(auth_app.fast)
    assert c.post("/api/auth/login",
                  json={"username": "admin", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login",
                  json={"username": "nobody", "password": "x"}).status_code == 401


def test_login_disabled_user_401(auth_app):
    c = _client(auth_app.fast)
    assert c.post("/api/auth/login",
                  json={"username": "dev2", "password": "devpass2"}).status_code == 401


def test_login_exposes_role_scoped_permissions(auth_app):
    body = _client(auth_app.fast).post(
        "/api/auth/login", json={"username": "dev1", "password": "devpass1"}
    ).json()
    perms = body["permissions"]
    assert "projects:view" in perms
    assert "users:manage" not in perms
    assert "roles:manage" not in perms


def test_issue_and_decode_token(auth_app):
    payload = jwt.decode(issue_token(SECRET, sub=str(auth_app.admin.id)), SECRET,
                         algorithms=["HS256"])
    assert payload["sub"] == str(auth_app.admin.id)
    assert payload["exp"] > int(datetime.now(UTC).timestamp())


def test_secure_requires_bearer(auth_app):
    assert _client(auth_app.fast).get("/api/secure").status_code == 401
    assert _client(auth_app.fast, "garbage").get("/api/secure").status_code == 401


def test_secure_accepts_valid_token(auth_app):
    r = _client(auth_app.fast, auth_app.token).get("/api/secure")
    assert r.status_code == 200
    assert r.json() == {"user": "admin", "id": auth_app.admin.id}


def test_expired_token_rejected(auth_app):
    expired = jwt.encode(
        {"sub": str(auth_app.admin.id), "exp": datetime.now(UTC) - timedelta(hours=1)},
        SECRET, algorithm="HS256",
    )
    assert _client(auth_app.fast, expired).get("/api/secure").status_code == 401


def test_disabled_user_token_rejected(auth_app):
    """禁用用户既有 token：get_current_user 查库发现 enabled=False → 401。"""
    h = auth_app
    import asyncio

    async def _disable():
        s = session_factory(h.engine)
        async with s() as sess:
            dev = (await sess.execute(
                select(User).where(User.username == "dev1"))).scalar_one()
            dev.enabled = False
            await sess.commit()
            return dev.id

    uid = asyncio.run(_disable())
    tok = issue_token(SECRET, sub=str(uid))
    assert _client(h.fast, tok).get("/api/secure").status_code == 401
    assert _client(h.fast).post(
        "/api/auth/login", json={"username": "dev1", "password": "devpass1"}
    ).status_code == 401


def test_auth_me_returns_user_and_permissions(auth_app):
    r = _client(auth_app.fast, auth_app.token).get("/api/auth/me")
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["username"] == "admin"
    assert "users:manage" in data["permissions"]

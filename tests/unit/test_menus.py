"""菜单接口测试（F5.11 延伸）：按权限过滤 + 空组剔除 + 隐藏路由并入 names。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai import security
from codereview_ai.api.auth import issue_token
from codereview_ai.api.menus import router as menus_router
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import Role, User


@pytest.fixture
def menus_app(tmp_path):
    """已播种的 app：admin（超管）+ dev1（developer 角色）。"""
    from tests.unit.helpers import make_admin_app

    async def seed(s):
        role = (await s.execute(
            select(Role).where(Role.builtin_code == "developer")
        )).scalar_one()
        s.add(User(username="dev1", password_hash=security.hash_password("devpass1"),
                   display_name="dev1", enabled=True, role_id=role.id))
        await s.flush()

    fast, token, admin, engine = asyncio.run(
        make_admin_app(tmp_path, db_name="menus.db", routers=[menus_router], seed=seed)
    )
    yield fast, token, admin, engine
    asyncio.run(engine.dispose())


def _client(fast, token: str) -> Iterator:
    return TestClient(fast, headers={"Authorization": f"Bearer {token}"})


def _dev_token(fast, engine) -> str:
    async def _q() -> int:
        async with session_factory(engine)() as s:
            return (await s.execute(
                select(User).where(User.username == "dev1"))).scalar_one().id

    return issue_token("s", sub=str(asyncio.run(_q())))


def test_menus_superadmin_sees_everything(menus_app):
    fast, token, _admin, _engine = menus_app
    data = _client(fast, token).get("/api/menus").json()
    names = data["names"]
    for expected in ("Dashboard", "Workrate", "Members", "Projects", "Reviews",
                     "Schedules", "Models", "Notifiers", "Caches",
                     "Settings", "Users", "Roles", "ReviewDetail", "ReviewConversation"):
        assert expected in names
    assert {g["key"] for g in data["groups"]} == {"analytics", "review", "resource", "system"}


def test_menus_developer_filtered_and_empty_groups_dropped(menus_app):
    fast, _token, _admin, engine = menus_app
    data = _client(fast, _dev_token(fast, engine)).get("/api/menus").json()
    names = data["names"]
    # developer: projects:view + reviews:view + stats:view
    for expected in ("Dashboard", "Workrate", "Members", "Projects", "Reviews",
                     "ReviewDetail", "ReviewConversation"):
        assert expected in names
    for forbidden in ("Schedules", "Models", "Notifiers", "Caches",
                      "Settings", "Users", "Roles"):
        assert forbidden not in names
    # 无 resource / system 可见项 → 整组不返回
    assert {g["key"] for g in data["groups"]} == {"analytics", "review"}


def test_menus_requires_auth(menus_app):
    fast, _token, _admin, _engine = menus_app
    assert TestClient(fast).get("/api/menus").status_code == 401

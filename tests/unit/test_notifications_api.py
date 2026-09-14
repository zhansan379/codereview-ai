"""系统消息 REST 权限口径：登录即可用（不再要求 `settings:manage`）。

背景：系统消息是「后台异步结果 → 用户」的提醒通道（重发成败等），全局广播、SSE 流
本就只验 token；REST 若挂 settings:manage，实际触发重发的 tech_lead 反而收不到结果、
确认接口也 403。SSE 连接建立时按库校验用户可用（禁用即 401）。离线临时 SQLite。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai.api.admin import notifications as notifications_router
from codereview_ai.api.auth import issue_token
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import Role, User
from codereview_ai.storage.system_notification_repo import SystemNotificationRepository
from tests.unit.helpers import make_admin_app


@pytest.fixture
def ctx(tmp_path):
    fast, token, _admin, engine = asyncio.run(make_admin_app(
        tmp_path, db_name="notif.db",
        routers=[notifications_router.router, notifications_router.sse_router]))
    holder = type("C", (), {"fast": fast, "token": token, "engine": engine})()
    yield holder
    asyncio.run(engine.dispose())


def _seed_notification(ctx) -> None:
    async def _go():
        async with session_factory(ctx.engine)() as s:
            await SystemNotificationRepository(s).create(
                type="redeliver_done", title="t", message="m", level="success", extra_data={})
            await s.commit()
    asyncio.run(_go())


def _dev_token(ctx) -> str:
    """developer 内置角色（无 settings:manage）用户的 token。"""
    async def _go():
        async with session_factory(ctx.engine)() as s:
            dev_role = (await s.execute(
                select(Role).where(Role.builtin_code == "developer"))).scalar_one()
            u = User(username="dev1", password_hash="x", enabled=True, role_id=dev_role.id)
            s.add(u)
            await s.commit()
            return issue_token("s", sub=str(u.id))
    return asyncio.run(_go())


def _disable_dev_user(ctx) -> None:
    async def _go():
        async with session_factory(ctx.engine)() as s:
            u = (await s.execute(select(User).where(User.username == "dev1"))).scalar_one()
            u.enabled = False
            await s.commit()
    asyncio.run(_go())


def test_list_and_ack_available_to_plain_user(ctx):
    """登录即可读写系统消息：无 settings:manage 的 developer 用户也能列出/确认。"""
    _seed_notification(ctx)
    c = TestClient(ctx.fast, headers={"Authorization": f"Bearer {_dev_token(ctx)}"})
    r = c.get("/api/notifications")
    assert r.status_code == 200 and r.json()["total"] == 1
    nid = r.json()["items"][0]["id"]
    assert c.post(f"/api/notifications/{nid}/acknowledge").status_code == 200
    assert c.get("/api/notifications").json()["total"] == 0


def test_requires_login(ctx):
    """未登录仍 401（放开的是权限门槛，不是鉴权）。"""
    assert TestClient(ctx.fast).get("/api/notifications").status_code == 401


def test_sse_rejects_disabled_user(ctx):
    """SSE 连接建立时按库校验用户可用性：禁用用户持有效 token 也 401。"""
    token = _dev_token(ctx)
    _disable_dev_user(ctx)
    assert TestClient(ctx.fast).get(
        f"/api/notifications/stream?token={token}").status_code == 401

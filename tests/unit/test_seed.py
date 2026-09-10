"""RBAC 种子（`storage/seed.py`）：幂等、FK 顺序、默认 admin 派生自 CR_ADMIN_PASSWORD。

离线临时 SQLite；`seed_rbac` 直接调起（不经 lifespan）。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select

from codereview_ai.security import verify_password
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import (
    Permission,
    ProjectMember,
    Role,
    RolePermission,
    User,
)
from codereview_ai.storage.seed import DEFAULT_ROLES, PERMISSION_CATALOG, seed_rbac


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def engine(tmp_path) -> AsyncIterator:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    await init_db(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_seed_creates_admin_roles_permissions(engine):
    async with session_factory(engine)() as s:
        await seed_rbac(s, "hunter2")

    async with session_factory(engine)() as s:
        users = (await s.execute(select(User))).scalars().all()
        assert len(users) == 1
        admin = users[0]
        assert admin.username == "admin"
        assert verify_password("hunter2", admin.password_hash)
        assert admin.role.is_super

        roles = (await s.execute(select(Role))).scalars().all()
        assert len(roles) == len(DEFAULT_ROLES)  # 四套内置角色
        assert {r.builtin_code for r in roles} == set(DEFAULT_ROLES)

        perms = (await s.execute(select(Permission))).scalars().all()
        assert len(perms) == len(PERMISSION_CATALOG)
        assert {p.code for p in perms} == {c for c, _, _, _ in PERMISSION_CATALOG}

        junction = (await s.execute(select(RolePermission))).scalars().all()
        admin_role = admin.role
        admin_perm_ids = {rp.permission_id for rp in junction if rp.role_id == admin_role.id}
        assert len(admin_perm_ids) == len(PERMISSION_CATALOG)  # admin 全量


async def test_seed_idempotent(engine):
    async with session_factory(engine)() as s:
        await seed_rbac(s, "hunter2")
    async with session_factory(engine)() as s:
        await seed_rbac(s, "other")

    async with session_factory(engine)() as s:
        assert (await s.execute(select(func.count()).select_from(User))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Role))).scalar_one() == len(DEFAULT_ROLES)  # noqa: E501
        assert (await s.execute(
            select(func.count()).select_from(Permission)
        )).scalar_one() == len(PERMISSION_CATALOG)
        # junction 无重复（复合唯一键兜底）
        rows = (await s.execute(select(RolePermission.role_id, RolePermission.permission_id))).all()
        assert len(rows) == len(set(rows))


async def test_seed_does_not_touch_existing_users(engine):
    from codereview_ai.storage.seed import seed_rbac as seed_again

    async with session_factory(engine)() as s:
        await seed_rbac(s, "hunter2")
        # 稍后手工造一个非 admin 用户，再跑 seed 应整体跳过
        role = (await s.execute(select(Role).where(Role.builtin_code == "developer"))).scalar_one()
        s.add(User(username="dev1", password_hash="x", display_name="D", role_id=role.id))
        await s.commit()

    async with session_factory(engine)() as s:
        await seed_again(s, "hunter2")

    async with session_factory(engine)() as s:
        users = (await s.execute(select(User))).scalars().all()
        assert len(users) == 2  # admin + dev1，没重复建 admin


async def test_project_member_table_exists(engine):
    """project_member 表存在且可查（隔离用）。空库无成员。"""
    async with session_factory(engine)() as s:
        assert (await s.execute(select(func.count()).select_from(ProjectMember))).scalar_one() == 0

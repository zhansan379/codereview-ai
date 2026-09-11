"""RBAC 测试共享设施：构建「已播种 admin」的离线 FastAPI 应用。

admin 为超管（is_super）→ 现有 CRUD/聚合测试在接入 RBAC 后仍全部放行，
无需改断言。`make_admin_app` 返回 `(fast, token, admin, engine)`：
- `fast`：已挂 auth + 指定 admin 路由，`state.engine/settings` 就绪；
- `token`：以 admin 用户 id 签发的 JWT；
- `admin`：seed 出的 admin User 行（超管）；
- `engine`：临时 SQLite（调用方负责 dispose）。
调用方如需额外 `app.state`（config_repository/forge_registry 等）在返回后自行设置。
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI
from sqlalchemy import select

from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import User
from codereview_ai.storage.seed import seed_rbac


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


Seed = Callable[[Any], Awaitable[None]]


async def make_admin_app(
    tmp_path,
    *,
    db_name: str = "rbac.db",
    routers: list[APIRouter] | None = None,
    seed: Seed | None = None,
    password: str = "hunter2",
) -> tuple[FastAPI, str, User, Any]:
    """建临时 SQLite + `init_db` + `seed_rbac`，可选 `seed(session)` 播种业务数据，
    返回挂好 auth/admin 路由的 FastAPI、admin token、admin 行与 engine。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / db_name}")
    await init_db(engine)
    settings = type("S", (), {
        "secret_key": "s", "encryption_key": _fernet_key(),
        "push_review_enabled": False,  # §7.7 全局默认 env；无落库行时回落此值
        "mr_review_enabled": False,    # §7.7 MR 轨全局默认 env（与 push 对称）
        "poll_include_closed": False,  # §9 补拉范围全局默认 env（无落库行时回落此值）
    })()
    async with session_factory(engine)() as s:
        await seed_rbac(s, password)
        if seed is not None:
            await seed(s)
        await s.commit()  # 确保调用方播种的业务数据落库
        admin = (await s.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        admin_id = admin.id

    fast = FastAPI()
    fast.state.engine = engine
    fast.state.settings = settings
    fast.include_router(auth_router, prefix="/api")
    for r in routers or []:
        fast.include_router(r, prefix="/api")
    token = issue_token(settings.secret_key, sub=str(admin_id))
    return fast, token, admin, engine

"""租户成本聚合测试：`GET /api/usage` 按 workspace/project 归因 `model_usage` 成本。

离线：`make_admin_app` seed 出 admin（超管）+ 一个 member（owner1，拥有 ws1）；两个 workspace
各自一个项目各一条审查任务 + 对应 `model_usage` 用量行（cost=1.0 / cost=2.0）。覆盖：
- 超管看全量：两个 workspace 成本合计 3.0，按项目分桶正确；
- 非超管（owner1）只见自有 ws1（成本 1.0，1 项目），看不到 ws2；
- 超管指定 `workspace_id` 收敛到单空间，越界 `workspace_id` 对非超管 → 404。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai.api.admin import usage
from codereview_ai.api.auth import issue_token
from codereview_ai.security import hash_password
from codereview_ai.storage.models import (
    ModelUsage,
    Project,
    ReviewTask,
    Role,
    User,
    Workspace,
)
from tests.unit.helpers import make_admin_app


async def _mk(tmp_path):
    owner_id = {}
    ws_ids = {}

    async def seed(s):
        role = (await s.execute(
            select(Role).where(Role.builtin_code == "member"))).scalar_one()
        owner1 = User(username="owner1", password_hash=hash_password("pw"),
                      display_name="Owner1", enabled=True, role_id=role.id)
        s.add(owner1)
        await s.flush()
        owner_id["uid"] = owner1.id

        ws1 = Workspace(name="own", slug="own", owner_id=owner1.id)
        ws2 = Workspace(name="other", slug="other", owner_id=None)
        s.add_all([ws1, ws2])
        await s.flush()
        ws_ids.update({"ws1": ws1.id, "ws2": ws2.id})

        p1 = Project(provider="gitlab", repo_id="a", repo_full_name="me/alpha",
                     enabled=True, workspace_id=ws1.id)
        p2 = Project(provider="github", repo_id="b", repo_full_name="other/beta",
                     enabled=True, workspace_id=ws2.id)
        s.add_all([p1, p2])
        await s.flush()

        t1 = ReviewTask(provider="gitlab", repo_id="a", event_type="mr", head_sha="h1",
                        state="completed", project_id=p1.id)
        t2 = ReviewTask(provider="github", repo_id="b", event_type="mr", head_sha="h2",
                        state="completed", project_id=p2.id)
        s.add_all([t1, t2])
        await s.flush()

        s.add(ModelUsage(task_id=t1.id, model="m1", prompt_tokens=100,
                         completion_tokens=50, total_tokens=150, cost=1.0))
        s.add(ModelUsage(task_id=t2.id, model="m2", prompt_tokens=200,
                         completion_tokens=100, total_tokens=300, cost=2.0))
        s.add(ModelUsage(task_id=t1.id, model="m1", prompt_tokens=10,
                         completion_tokens=5, total_tokens=15, cost=0.5))
        # 不可归因用量（task_id 缺失）应被排除
        s.add(ModelUsage(task_id=None, model="m0", total_tokens=999, cost=99.0))

    fast, admin_token, _admin, engine = await make_admin_app(
        tmp_path, db_name="usage.db", routers=[usage.router], seed=seed)
    owner_token = issue_token("s", sub=str(owner_id["uid"]))
    return fast, engine, admin_token, owner_token, ws_ids


def _get(fast, token, **params):
    headers = {"Authorization": f"Bearer {token}"}
    return TestClient(fast, headers=headers).get("/api/usage", params=params)


@pytest.mark.asyncio
async def test_super_sees_all_workspaces(tmp_path):
    fast, engine, admin_tok, _owner, ws_ids = await _mk(tmp_path)
    try:
        r = _get(fast, admin_tok)
        assert r.status_code == 200, r.text
        data = r.json()
        # 两个 workspace 成本合计 3.5（t1 两笔 1.0+0.5 + t2 一笔 2.0）；任务缺失的 99.0 被排除
        assert data["cost"] == 3.5
        ws = {w["workspace_name"]: w for w in data["workspaces"]}
        assert set(ws) == {"own", "other"}
        assert ws["own"]["cost"] == 1.5
        assert ws["own"]["requests"] == 2
        assert ws["other"]["cost"] == 2.0
        own_proj = ws["own"]["projects"][0]
        assert own_proj["project_name"] == "me/alpha"
        assert own_proj["total_tokens"] == 165
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_member_sees_only_own_workspace(tmp_path):
    fast, engine, _admin, owner_tok, _ws = await _mk(tmp_path)
    try:
        r = _get(fast, owner_tok)
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["workspaces"]) == 1
        assert data["workspaces"][0]["workspace_name"] == "own"
        assert data["cost"] == 1.5
        assert [p["project_name"] for p in data["workspaces"][0]["projects"]] == ["me/alpha"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_super_workspace_filter_and_member_cross_boundary(tmp_path):
    fast, engine, admin_tok, owner_tok, ws_ids = await _mk(tmp_path)
    try:
        # 超管指定 workspace_id → 只收敛到该空间
        r = _get(fast, admin_tok, workspace_id=ws_ids["ws1"])
        assert r.status_code == 200
        assert r.json()["cost"] == 1.5
        assert len(r.json()["workspaces"]) == 1

        # 超管收敛到 ws2
        r2 = _get(fast, admin_tok, workspace_id=ws_ids["ws2"])
        assert r2.json()["cost"] == 2.0

        # 非超管看别人空间 → 404（即使带合法 id 也会被 owner 门拦下）

        r3 = _get(fast, owner_tok, workspace_id=ws_ids["ws2"])
        assert r3.status_code == 404
    finally:
        await engine.dispose()

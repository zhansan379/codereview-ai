"""RBAC 行为测试（F5.11）：拒绝/放行矩阵 + 项目级数据隔离 + 成员管理。

离线：`make_admin_app` seed 出 admin（超管）+ dev1(developer)/view1(viewer)/tl1(tech_lead)
与两个项目 P1(成员 dev/view)/P2，审查任务分属两项目。逐角色用 token 打各端点断言。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai.api.admin import projects, reviews, stats
from codereview_ai.api.auth import issue_token
from codereview_ai.security import hash_password
from codereview_ai.storage.models import (
    Project,
    ProjectMember,
    ReviewFinding,
    ReviewTask,
    Role,
    User,
)
from tests.unit.helpers import make_admin_app


class _Ctx:
    pass


async def _mk_ctx(tmp_path) -> _Ctx:
    ctx = _Ctx()
    routers = [projects.router, reviews.router, stats.router]

    async def seed(s):
        async def _user(username, builtin):
            role = (await s.execute(
                select(Role).where(Role.builtin_code == builtin))).scalar_one()
            u = User(username=username, password_hash=hash_password("pw"),
                     display_name=username, enabled=True, role_id=role.id)
            s.add(u)
            await s.flush()
            return u

        dev = await _user("dev1", "developer")
        view = await _user("view1", "viewer")
        tl = await _user("tl1", "tech_lead")
        ctx.uid_dev, ctx.uid_view, ctx.uid_tl = dev.id, view.id, tl.id

        p1 = Project(provider="gitlab", repo_id="1", repo_full_name="o/r1", enabled=True)
        p2 = Project(provider="github", repo_id="2", repo_full_name="o2/r2", enabled=True)
        s.add_all([p1, p2])
        await s.flush()
        ctx.pid1, ctx.pid2 = p1.id, p2.id
        s.add_all([
            ProjectMember(project_id=p1.id, user_id=dev.id),
            ProjectMember(project_id=p1.id, user_id=view.id),
        ])

        t1 = ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                        head_sha="h1", state="completed", project_id=p1.id)
        t2 = ReviewTask(provider="github", repo_id="2", pr_number=2, event_type="mr",
                        head_sha="h2", state="completed", project_id=p2.id)
        s.add_all([t1, t2])
        await s.flush()
        ctx.tid1, ctx.tid2 = t1.id, t2.id
        s.add(ReviewFinding(task_id=t1.id, fingerprint="f1", severity="high",
                            category="bug", file="a.py", status="active"))

    fast, admin_token, admin, engine = await make_admin_app(
        tmp_path, db_name="rbac.db", routers=routers, seed=seed)
    ctx.fast, ctx.engine, ctx.admin_token = fast, engine, admin_token
    ctx.tok_dev = issue_token("s", sub=str(ctx.uid_dev))
    ctx.tok_view = issue_token("s", sub=str(ctx.uid_view))
    ctx.tok_tl = issue_token("s", sub=str(ctx.uid_tl))
    return ctx


@pytest.fixture
def ctx(tmp_path):
    holder = asyncio.run(_mk_ctx(tmp_path))
    yield holder
    asyncio.run(holder.engine.dispose())


def _c(ctx, token: str | None = None) -> Iterator:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(ctx.fast, headers=headers)


# ── 拒绝/放行矩阵 ──────────────────────────────────────────────────────────

def test_developer_cannot_create_project(ctx):
    assert _c(ctx, ctx.tok_dev).post(
        "/api/projects", json={"provider": "gitlab", "repo_id": "3"}
    ).status_code == 403  # 无 projects:manage


def test_developer_sees_only_member_projects(ctx):
    ids = {p["id"] for p in _c(ctx, ctx.tok_dev).get("/api/projects").json()}
    assert ids == {ctx.pid1}  # 只见 P1，不见 P2


def test_developer_cannot_read_nonmember_project_detail(ctx):
    assert _c(ctx, ctx.tok_dev).get(f"/api/projects/{ctx.pid2}").status_code == 403


def test_developer_cannot_manage_nonmember_project(ctx):
    # 非成员改 P2 → 403
    assert _c(ctx, ctx.tok_dev).delete(f"/api/projects/{ctx.pid2}").status_code == 403


def test_project_review_isolation(ctx):
    """dev 只见 P1 的审查记录（含详情），P2 的不可见。"""
    ids = {r["id"] for r in _c(ctx, ctx.tok_dev).get("/api/reviews").json()["items"]}
    assert ids == {ctx.tid1}
    assert _c(ctx, ctx.tok_dev).get(f"/api/reviews/{ctx.tid2}").status_code == 404


def test_viewer_cannot_update_finding_status(ctx):
    """viewer 无 reviews:update → 403；dev 有 → 200。"""
    c = _c(ctx, ctx.tok_view)
    assert c.post("/api/reviews/findings/1/status", json={"status": "waived"}).status_code == 403


def test_developer_can_update_finding_status(ctx):
    r = _c(ctx, ctx.tok_dev).post(
        f"/api/reviews/findings/{_finding_id(ctx)}/status", json={"status": "waived"})
    assert r.status_code == 200
    assert r.json()["status"] == "waived"


def _finding_id(ctx) -> int:
    async def _q():
        s = ctx.engine
        from codereview_ai.storage.db import session_factory
        ss = session_factory(s)
        async with ss() as sess:
            return (await sess.execute(
                select(ReviewFinding.id).where(ReviewFinding.task_id == ctx.tid1))).scalar_one()
    return asyncio.run(_q())


def test_stats_scoped_to_membership(ctx):
    d = _c(ctx, ctx.tok_dev).get("/api/stats").json()
    assert d["total_tasks"] == 1  # 只见 P1 的一条任务


# ── 角色能力差异 ───────────────────────────────────────────────────────────

def test_techlead_sees_all_projects(ctx):
    ids = {p["id"] for p in _c(ctx, ctx.tok_tl).get("/api/projects").json()}
    assert ids == {ctx.pid1, ctx.pid2}


def test_admin_super_bypass(ctx):
    ids = {p["id"] for p in _c(ctx, ctx.admin_token).get("/api/projects").json()}
    assert ids == {ctx.pid1, ctx.pid2}
    assert _c(ctx, ctx.admin_token).delete(f"/api/projects/{ctx.pid2}").status_code == 204


# ── 成员管理端点 ───────────────────────────────────────────────────────────

def test_member_endpoints_require_projects_manage(ctx):
    assert _c(ctx, ctx.tok_dev).put(
        f"/api/projects/{ctx.pid1}/members", json={"user_ids": [ctx.uid_dev]}
    ).status_code == 403  # dev 无 projects:manage


def test_admin_set_members_grants_visibility(ctx):
    # dev 先只见 P1；admin 把 dev 加入 P2 → dev 随后可见 P2
    before = {p["id"] for p in _c(ctx, ctx.tok_dev).get("/api/projects").json()}
    assert before == {ctx.pid1}
    r = _c(ctx, ctx.admin_token).put(
        f"/api/projects/{ctx.pid2}/members", json={"user_ids": [ctx.uid_dev]})
    assert r.status_code == 200
    after = {p["id"] for p in _c(ctx, ctx.tok_dev).get("/api/projects").json()}
    assert after == {ctx.pid1, ctx.pid2}


def test_member_list_returns_users(ctx):
    rows = _c(ctx, ctx.admin_token).get(f"/api/projects/{ctx.pid1}/members").json()
    usernames = {r["username"] for r in rows}
    assert usernames >= {"dev1", "view1"}

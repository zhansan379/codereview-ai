"""后台管理 REST 测试（DESIGN §14）：projects / models CRUD + Fernet 脱敏 + 连通测试。

离线：临时 SQLite + httpx ASGI；JWT 由 auth.issue_token 签发；探测函数 monkeypatch 掉即零网络。
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.admin import forges, notifiers, projects, pull, reviews, tasks
from codereview_ai.api.admin import models as admin_models
from codereview_ai.api.admin import notifier_members
from codereview_ai.api.admin import settings as admin_settings
from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ForgeConfig, ModelConfig, ReviewFinding, ReviewTask


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}"
    engine = create_engine(url)
    await init_db(engine)

    settings = type("S", (), {
        "secret_key": "s", "encryption_key": _fernet_key(),
        "push_review_enabled": False,  # §7.7 全局默认 env；无落库行时回落此值
        "mr_review_enabled": False,    # §7.7 MR 轨全局默认 env（与 push 对称）
    })()

    fast = FastAPI()
    fast.state.engine = engine
    fast.state.settings = settings
    fast.state.config_repository = ConfigRepository(engine, encryption_key=_fernet_key())
    fast.state.forge_registry = None  # 单测不启动 worker；热更分支被跳过
    fast.include_router(auth_router, prefix="/api")
    fast.include_router(projects.router, prefix="/api")
    fast.include_router(admin_models.router, prefix="/api")
    fast.include_router(notifier_members.router, prefix="/api")
    fast.include_router(notifiers.router, prefix="/api")
    fast.include_router(forges.router, prefix="/api")
    fast.include_router(reviews.router, prefix="/api")
    fast.include_router(tasks.router, prefix="/api")
    fast.include_router(pull.router, prefix="/api")
    fast.include_router(admin_settings.router, prefix="/api")

    token = issue_token(settings.secret_key)
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _client(fast: FastAPI, token: str) -> TestClient:
    return TestClient(fast, headers={"Authorization": f"Bearer {token}"})


def test_projects_crud_roundtrip(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.post("/api/projects", json={
            "provider": "gitlab", "repo_id": "123", "repo_full_name": "a/b",
            "branch_rule": "main", "review_strategy": "diff", "score_threshold": 90,
            "enforce_score_threshold": True,
        })
        assert r.status_code == 201
        pid = r.json()["id"]
        assert r.json()["score_threshold"] == 90
        assert r.json()["enforce_score_threshold"] is True  # 新建透传

        # 无 token → 401
        assert TestClient(fast).get("/api/projects").status_code == 401

        lst = c.get("/api/projects").json()
        assert len(lst) == 1 and lst[0]["repo_full_name"] == "a/b"

        # 重复 (provider, repo_id) 新建 → 409 友好提示而非 500
        dup = c.post("/api/projects", json={"provider": "gitlab", "repo_id": "123"})
        assert dup.status_code == 409
        assert "已存在" in dup.json()["detail"]

        upd = c.put(f"/api/projects/{pid}", json={
            "provider": "gitlab", "repo_id": "123", "branch_rule": "dev",
            "score_threshold": 60, "enforce_score_threshold": True,
            "push_enabled": True, "push_branch_globs": "main,release/*",
            "mr_enabled": False,
        }).json()
        assert upd["branch_rule"] == "dev" and upd["score_threshold"] == 60
        assert upd["enforce_score_threshold"] is True
        assert upd["push_enabled"] is True and upd["push_branch_globs"] == "main,release/*"
        assert upd["mr_enabled"] is False

        # push_enabled / mr_enabled 可回写为 null → 继承全局默认
        upd2 = c.put(f"/api/projects/{pid}", json={
            "provider": "gitlab", "repo_id": "123", "push_enabled": None, "mr_enabled": None,
        }).json()
        assert upd2["push_enabled"] is None and upd2["mr_enabled"] is None

        assert c.get("/api/projects/9999").status_code == 404
        assert c.delete(f"/api/projects/{pid}").status_code == 204
        assert c.get("/api/projects").json() == []


def test_models_create_masks_and_reads_encrypted(app):
    fast, token = app
    fast.state.settings.encryption_key = _fernet_key()
    with _client(fast, token) as c:
        r = c.post("/api/models", json={
            "name": "deepseek", "provider": "deepseek", "model": "deepseek-chat",
            "api_key": "sk-secret-abc", "base_url": "https://x", "priority": 5,
        })
        assert r.status_code == 201, r.text
        mid = r.json()["id"]
        # 读路径统一回显 ******
        assert r.json()["api_key"] == "******"

        # 落库是密文、明文不出现
        found: list[ModelConfig] = []

        async def _fetch() -> None:
            from sqlalchemy import select

            session = session_factory(fast.state.engine)
            async with session() as s:
                found.append((await s.execute(select(ModelConfig))).scalar_one())

        import asyncio

        asyncio.get_event_loop().run_until_complete(_fetch())
        row = found[0]
        assert row.api_key_encrypted != "sk-secret-abc"
        assert "sk-secret-abc" not in row.api_key_encrypted
        from codereview_ai.crypto import decrypt

        assert decrypt(row.api_key_encrypted, _fernet_key()) == "sk-secret-abc"

        # 列表与详情回显掩码
        assert c.get("/api/models").json()[0]["api_key"] == "******"
        assert c.get(f"/api/models/{mid}").json()["api_key"] == "******"

        # 更新：掩码表示保留原文；明文则重加密
        upd = c.put(f"/api/models/{mid}", json={
            "name": "deepseek", "model": "deepseek-chat", "api_key": "******", "priority": 9,
        }).json()
        assert upd["priority"] == 9 and upd["api_key"] == "******"
        upd2 = c.put(f"/api/models/{mid}", json={
            "name": "deepseek", "model": "deepseek-chat", "api_key": "sk-new-xyz",
        }).json()
        assert upd2["api_key"] == "******"


def test_models_probe_with_injected_probe(app, monkeypatch):
    fast, token = app
    with _client(fast, token) as c:
        c.post("/api/models", json={"name": "m", "provider": "deepseek",
                                    "model": "deepseek-chat", "api_key": "sk-x"})
        mid = c.get("/api/models").json()[0]["id"]

        async def fake_probe(cfg, prompt, **kw) -> bool:
            assert kw["encryption_key"]
            return True

        monkeypatch.setattr(admin_models, "probe_model", fake_probe)
        assert c.post(f"/api/models/{mid}/test", json={"prompt": "hi"}).status_code == 200

        async def fail_probe(cfg, prompt, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(admin_models, "probe_model", fail_probe)
        assert c.post(f"/api/models/{mid}/test", json={}).status_code == 502


def test_models_missing_api_key_400_on_create(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.post("/api/models", json={
            "name": "bad", "provider": "x", "model": "x", "api_key": "******",
        })
        assert r.status_code == 400


def test_notifiers_crud_and_masking(app):
    fast, token = app
    with _client(fast, token) as c:
        # 先建系统级成员，拿到 @ 绑定的 member_id
        alice = c.post("/api/notifiers/members", json={
            "name": "Alice", "git_username": "alice", "dingtalk_mobile": "13800000000",
        }).json()
        bob = c.post("/api/notifiers/members", json={
            "name": "Bob", "git_username": "bob", "wecom_userid": "wbob",
        }).json()
        r = c.post("/api/notifiers", json={
            "channel": "dingtalk", "webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "secret": "SECsecret", "project_id": None, "at_threshold": 60, "at_all": True,
            "at_member_ids": [alice["id"], bob["id"]],
        })
        assert r.status_code == 201, r.text
        nid = r.json()["id"]
        assert r.json()["webhook"] == "******" and r.json()["secret"] == "******"
        assert r.json()["channel"] == "dingtalk" and r.json()["project_id"] is None
        assert r.json()["at_all"] is True
        assert r.json()["at_member_ids"] == [alice["id"], bob["id"]]

        upd = c.put(f"/api/notifiers/{nid}", json={
            "channel": "dingtalk", "webhook": "******", "secret": "******",
            "project_id": 3, "at_threshold": 80, "at_all": False,
            "at_member_ids": [alice["id"]],
        }).json()
        assert upd["project_id"] == 3 and upd["at_threshold"] == 80
        assert upd["at_all"] is False
        assert upd["at_member_ids"] == [alice["id"]]

        assert c.get("/api/notifiers").json()[0]["webhook"] == "******"
        assert c.delete(f"/api/notifiers/{nid}").status_code == 204
        assert c.delete(f"/api/notifiers/{nid}").status_code == 404


def test_push_review_default_api_roundtrip(app):
    """§7.7 全局自动审查默认开关 API：初始回落 env、POST 落库、GET 读回 db 与紧源码。"""
    fast, token = app
    with _client(fast, token) as c:
        # 无落库行 → 回落 env 默认（settings.push_review_enabled=False），source=env
        g = c.get("/api/settings/push-review-default").json()
        assert g == {"enabled": False, "source": "env"}

        # 开启并落库 → source=db，后续 GET 读回库值
        r = c.post("/api/settings/push-review-default", json={"enabled": True}).json()
        assert r == {"enabled": True, "source": "db"}
        assert c.get("/api/settings/push-review-default").json()["enabled"] is True

        # 关闭 → 库值覆盖，不回落 env
        r = c.post("/api/settings/push-review-default", json={"enabled": False}).json()
        assert r == {"enabled": False, "source": "db"}


def test_mr_review_default_api_roundtrip(app):
    """§7.7 MR 轨全局自动审查默认开关 API（与 push 对称）：初始回落 env、POST 落库、GET 读回。"""
    fast, token = app
    with _client(fast, token) as c:
        # 无落库行 → 回落 env 默认（settings.mr_review_enabled=False），source=env
        g = c.get("/api/settings/mr-review-default").json()
        assert g == {"enabled": False, "source": "env"}

        # 开启并落库 → source=db，后续 GET 读回库值
        r = c.post("/api/settings/mr-review-default", json={"enabled": True}).json()
        assert r == {"enabled": True, "source": "db"}
        assert c.get("/api/settings/mr-review-default").json()["enabled"] is True

        # 关闭 → 库值覆盖，不回落 env
        r = c.post("/api/settings/mr-review-default", json={"enabled": False}).json()
        assert r == {"enabled": False, "source": "db"}


def test_reviews_list_pagination_and_detail(app):
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            t1 = ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                            branch="main", head_sha="aaa", state="completed", score_total=80,
                            summary_md="# ok")
            t2 = ReviewTask(provider="gitlab", repo_id="1", pr_number=2, event_type="mr",
                            branch="main", head_sha="bbb", state="failed", error="boom")
            s.add_all([t1, t2])
            await s.flush()
            s.add(ReviewFinding(task_id=t1.id, fingerprint="h", severity="high",
                                category="bug", file="a.py", title="t"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    with _client(fast, token) as c:
        page = c.get("/api/reviews?state=completed&limit=10").json()
        assert page["total"] == 1 and len(page["items"]) == 1
        assert page["items"][0]["score_total"] == 80
        assert page["items"][0]["exec_mode"] is None

        all_page = c.get("/api/reviews?limit=1").json()
        assert all_page["total"] == 2 and len(all_page["items"]) == 1

        detail = c.get("/api/reviews/1").json()
        assert detail["state"] == "completed"
        assert detail["exec_mode"] is None
        assert len(detail["findings"]) == 1 and detail["findings"][0]["severity"] == "high"

        assert c.get("/api/reviews/999").status_code == 404


def test_reviews_export_xlsx_filters(app):
    """导出端点：按 state/severity/status 过滤生成 .xlsx，且不受分页限制。"""
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            t1 = ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                            branch="main", head_sha="aaa", state="completed",
                            score_total=80, summary_md="# ok")
            t2 = ReviewTask(provider="gitlab", repo_id="2", pr_number=2, event_type="mr",
                            branch="dev", head_sha="bbb", state="completed")
            s.add_all([t1, t2])
            await s.flush()
            s.add(ReviewFinding(task_id=t1.id, fingerprint="h", severity="high",
                                category="bug", file="a.py", title="t", status="active"))
            s.add(ReviewFinding(task_id=t1.id, fingerprint="m", severity="medium",
                                category="perf", file="b.py", title="mm", status="waived"))
            s.add(ReviewFinding(task_id=t2.id, fingerprint="c", severity="low",
                                category="style", file="c.py", title="cc", status="active"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    from io import BytesIO

    from openpyxl import load_workbook

    with _client(fast, token) as c:
        # 全量导出：三条问题都在，表头第一行。
        resp = c.get("/api/reviews/export")
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        assert 'attachment; filename="review_issues_' in resp.headers["content-disposition"]
        ws = load_workbook(BytesIO(resp.content)).active
        assert ws.max_row == 4  # 表头 + 3
        assert ws["A1"].value == "评审ID"
        # review 信息列已带上（PR号/仓库）；task_id DESC 所以 t2 在前、t1 high 在第三行。
        assert ws["D2"].value == 2 and ws["D3"].value == 1 and ws["G3"].value == "高"

        # 严重度+状态过滤 → 只剩 high/active 那条（t1 的第一条）。
        ws2 = load_workbook(BytesIO(c.get(
            "/api/reviews/export", params={"severities": "high", "statuses": "active"}
        ).content)).active
        assert ws2.max_row == 2 and ws2["G2"].value == "高" and ws2["L2"].value == "待处理"

        # state 过滤 → 只剩 t2 的那条（PR号=2）。
        ws3 = load_workbook(BytesIO(c.get(
            "/api/reviews/export", params={"state": "completed", "severities": "low"}
        ).content)).active
        assert ws3.max_row == 2 and ws3["D2"].value == 2


def test_reviews_list_extended_filters(app):
    """列表新增筛选：事件类型/平台/评分区间/完成时间范围，且导出沿用同一组顶层筛选。"""
    from datetime import UTC, datetime
    fast, token = app

    def _utc(s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=UTC)

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            t1 = ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                            branch="main", head_sha="aaa", state="completed",
                            score_total=90, finished_at=_utc("2026-09-01T10:00"))
            t2 = ReviewTask(provider="github", repo_id="2", pr_number=None, event_type="push",
                            branch="dev", head_sha="bbb", state="completed",
                            score_total=60, finished_at=_utc("2026-09-05T10:00"))
            t3 = ReviewTask(provider="gitlab", repo_id="1", pr_number=3, event_type="mr",
                            branch="main", head_sha="ccc", state="completed",
                            score_total=45, finished_at=_utc("2026-09-02T10:00"))
            s.add_all([t1, t2, t3])
            await s.flush()
            s.add(ReviewFinding(task_id=t1.id, fingerprint="a", severity="high",
                                category="bug", file="a.py", title="t", status="active"))
            s.add(ReviewFinding(task_id=t2.id, fingerprint="b", severity="medium",
                                category="perf", file="b.py", title="u", status="active"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    from io import BytesIO

    from openpyxl import load_workbook

    def _ids(resp_json) -> list[int]:
        return [it["id"] for it in resp_json["items"]]

    with _client(fast, token) as c:
        # 事件类型+平台 → 命中两条 mr/gitlab（t1、t3）；评分下限过滤后仅剩 t1(90)。
        page = c.get("/api/reviews", params={"event_type": "mr", "provider": "gitlab"}).json()
        assert page["total"] == 2
        page2 = c.get("/api/reviews", params={"event_type": "mr", "score_min": 50}).json()
        assert page2["total"] == 1 and page2["items"][0]["score_total"] == 90
        # 完成时间范围 → 仅 t2(09-05)。
        page3 = c.get("/api/reviews", params={"finished_from": "2026-09-03"}).json()
        assert page3["total"] == 1 and page3["items"][0]["score_total"] == 60
        # 评分区间 40-65 → 仅 t1? 不：t2(60) 与 t3(45)。
        page4 = c.get("/api/reviews", params={"score_min": 40, "score_max": 65}).json()
        assert {it["score_total"] for it in page4["items"]} == {45, 60}
        # 导出沿用顶层筛选：score>=50 → 2 行（t1/t2），非受分页限制。
        resp = c.get("/api/reviews/export", params={"score_min": 50})
        ws = load_workbook(BytesIO(resp.content)).active
        assert ws.max_row == 3  # 表头 + 2


def test_tasks_list_and_retry(app):
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=9, event_type="mr",
                             branch="main", head_sha="abc", state="failed", attempt=1,
                             error="timeout"))
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=8, event_type="mr",
                             branch="main", head_sha="def", state="completed"))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    with _client(fast, token) as c:
        failed = c.get("/api/tasks?state=failed").json()
        assert len(failed) == 1 and failed[0]["attempt"] == 1

        tid = failed[0]["id"]
        retried = c.post(f"/api/tasks/{tid}/retry").json()
        assert retried["state"] == "queued" and retried["attempt"] == 2

        # 已完成任务不能重试 → 409
        done_id = c.get("/api/tasks?state=completed").json()[0]["id"]
        assert c.post(f"/api/tasks/{done_id}/retry").status_code == 409
        assert c.post("/api/tasks/9999/retry").status_code == 404


def test_retry_reenqueues_payload(app):
    """重试失败任务时把原始 webhook 重新投进队列（修复原先一直排队中的 bug）。"""
    fast, token = app
    payload = '{"action": "open", "object_attributes": {"action": "open"}}'
    payload_bytes = payload.encode("utf-8")

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=7, event_type="mr",
                             branch="main", head_sha="xyz", state="failed", attempt=1,
                             error="boom", payload=payload))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    calls: list[tuple[str, bytes]] = []

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            calls.append((provider, raw))
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()

    with _client(fast, token) as c:
        failed = c.get("/api/tasks?state=failed").json()
        tid = failed[0]["id"]
        retried = c.post(f"/api/tasks/{tid}/retry").json()
        assert retried["state"] == "queued" and retried["attempt"] == 2
        assert calls == [("gitlab", payload_bytes)]


def test_tasks_retry_skipped_recoverable_only(app):
    """门控/配置类 skipped 可补审；删分支 skipped 有 head 可审，禁止重试（409）。"""
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p1", state="skipped",
                             skip_reason="push_disabled", error="push 审查未开启...",
                             payload='{"x":1}'))
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p2", state="skipped",
                             skip_reason="branch_deleted", error="push 事件为删除分支..."))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()

    with _client(fast, token) as c:
        lst = c.get("/api/tasks").json()
        disabled = next(t for t in lst if t["skip_reason"] == "push_disabled")
        deleted = next(t for t in lst if t["skip_reason"] == "branch_deleted")
        # skip_reason 已在列表响应露出，前端据此亮「补审」按钮
        assert disabled["skip_reason"] == "push_disabled"

        r = c.post(f"/api/tasks/{disabled['id']}/retry")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "queued"
        assert c.post(f"/api/tasks/{deleted['id']}/retry").status_code == 409
        # mr 轨：mr_disabled 门控类 skipped 可补审；非门控的 branch_mismatch 仍 409
        async def _mr() -> None:
            session = session_factory(fast.state.engine)
            async with session() as s:
                s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=5, event_type="mr",
                                 branch="main", head_sha="m1", state="skipped",
                                 skip_reason="mr_disabled", error="MR 自动审查未开启...",
                                 payload='{"x":1}'))
                s.add(ReviewTask(provider="gitlab", repo_id="1", pr_number=6, event_type="mr",
                                 branch="main", head_sha="m2", state="skipped",
                                 skip_reason="branch_mismatch"))
                await s.commit()
        asyncio.get_event_loop().run_until_complete(_mr())
        mrs = c.get("/api/tasks").json()
        disabled_mr = next(t for t in mrs if t["event_type"] == "mr" and t["skip_reason"] == "mr_disabled")
        other_mr = next(t for t in mrs if t["event_type"] == "mr" and t["skip_reason"] == "branch_mismatch")
        r = c.post(f"/api/tasks/{disabled_mr['id']}/retry")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "queued"
        assert c.post(f"/api/tasks/{other_mr['id']}/retry").status_code == 409


def test_retry_push_failed_sets_force_rerun(app):
    """push 轨失败任务重试后置 force_rerun，令 worker 绕过幂等预检真正重跑（修空转 bug）。"""
    fast, token = app

    async def _seed() -> None:
        session = session_factory(fast.state.engine)
        async with session() as s:
            s.add(ReviewTask(provider="gitlab", repo_id="1", event_type="push",
                             branch="main", head_sha="p3", state="failed", attempt=1,
                             error="boom", payload='{"x":1}'))
            await s.commit()

    import asyncio
    asyncio.get_event_loop().run_until_complete(_seed())

    class _FakeEnqueuer:
        async def enqueue(self, provider: str, raw: bytes) -> str:
            return "t-x"

    fast.state.enqueuer = _FakeEnqueuer()
    tid = None
    async def _get() -> None:
        nonlocal tid
        from sqlalchemy import select
        session = session_factory(fast.state.engine)
        async with session() as s:
            tid = (await s.execute(
                select(ReviewTask).where(ReviewTask.event_type == "push"))).scalar_one().id
    asyncio.get_event_loop().run_until_complete(_get())

    with _client(fast, token) as c:
        c.post(f"/api/tasks/{tid}/retry").json()

    async def _assert() -> None:
        from sqlalchemy import select
        session = session_factory(fast.state.engine)
        async with session() as s:
            row = (await s.execute(select(ReviewTask))).scalar_one()
            assert row.force_rerun is True  # push 重试写到 force_rerun，供 worker 绕过幂等
    asyncio.get_event_loop().run_until_complete(_assert())


def test_forges_list_synthesizes_defaults(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CR_GITLAB_TOKEN", raising=False)
    with _client(fast, token) as c:
        rows = c.get("/api/forges").json()
        assert {r["provider"] for r in rows} == {"github", "gitlab"}
        # 未配置：URL 回退默认、token 空、env_active False
        gh = next(r for r in rows if r["provider"] == "github")
        assert gh["url"] == "https://api.github.com" and gh["token"] == ""
        assert gh["env_active"] is False


def test_forges_upsert_encrypts_and_masks(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    with _client(fast, token) as c:
        r = c.put("/api/forges/github", json={"url": "https://gh.example", "token": "gh-secret-abc", "enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["token"] == "******"
        assert r.json()["url"] == "https://gh.example"

        # 落库密文，明文不出现
        found: list[ForgeConfig] = []

        async def _fetch() -> None:
            from sqlalchemy import select

            session = session_factory(fast.state.engine)
            async with session() as s:
                found.append((await s.execute(select(ForgeConfig))).scalar_one())

        import asyncio

        asyncio.get_event_loop().run_until_complete(_fetch())
        row = found[0]
        assert row.token_encrypted not in ("", "gh-secret-abc")
        from codereview_ai.crypto import decrypt

        assert decrypt(row.token_encrypted, _fernet_key()) == "gh-secret-abc"

        # 掩码提交 → 保留原密文；新明文 → 重加密
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "******", "enabled": True})
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "gh-new-xyz", "enabled": True})

        async def _last() -> None:
            nonlocal row
            from sqlalchemy import select

            async with session_factory(fast.state.engine)() as s:
                row = (await s.execute(select(ForgeConfig))).scalar_one()

        asyncio.get_event_loop().run_until_complete(_last())
        assert decrypt(row.token_encrypted, _fernet_key()) == "gh-new-xyz"


def test_forges_probe_zero_network_via_injected_probe(app, monkeypatch):
    fast, token = app
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    with _client(fast, token) as c:
        c.put("/api/forges/github", json={"url": "https://gh.example", "token": "tok", "enabled": True})

        captured: dict = {}

        async def fake_probe(provider, url, token):
            captured.update(provider=provider, url=url, token=token)
            from codereview_ai.forges.scopes import Capability

            return [
                Capability(name="connect", label="平台连通 / 认证", status="ok"),
                Capability(name="read_pull", label="拉取打开 PR/MR", status="missing",
                           detail="缺 scope：repo"),
            ]

        monkeypatch.setattr(forges, "probe_forge", fake_probe)
        r = c.post("/api/forges/github/test", json={})
        assert r.status_code == 200, r.text
        assert captured == {"provider": "github", "url": "https://gh.example", "token": "tok"}
        body = r.json()
        assert body["ok"] is True
        caps = {cap["name"]: cap["status"] for cap in body["capabilities"]}
        assert caps == {"connect": "ok", "read_pull": "missing"}
        # 连通 ok 但读权限缺失 → ok 仍为 True（连上了，但告知缺权限）
        async def read_only_probe(provider, url, token):
            from codereview_ai.forges.scopes import Capability

            return [Capability(name="connect", label="平台连通 / 认证", status="ok"),
                    Capability(name="read_pull", label="拉取打开 PR/MR", status="missing")]  # noqa: E501

        monkeypatch.setattr(forges, "probe_forge", read_only_probe)
        assert c.post("/api/forges/github/test", json={}).json()["ok"] is True

        async def fail_probe(provider, url, token):
            raise RuntimeError("boom")

        monkeypatch.setattr(forges, "probe_forge", fail_probe)
        assert c.post("/api/forges/github/test", json={}).status_code == 502


def test_forges_probe_invalid_provider_404(app):
    fast, token = app
    with _client(fast, token) as c:
        assert c.put("/api/forges/gitee", json={"url": "u", "token": "t"}).status_code == 404
        assert c.post("/api/forges/gitee/test", json={}).status_code == 404


def test_pull_poll_503_and_status_without_worker(app):
    """无 poller（worker 未启动）→ /pulls/poll 与 /pulls/poll/status 均 503。"""
    fast, token = app
    assert getattr(fast.state, "poller", None) is None
    with _client(fast, token) as c:
        r = c.post("/api/pulls/poll")
        assert r.status_code == 503
        assert "worker 未启动" in r.json()["detail"]
        assert c.get("/api/pulls/poll/status").status_code == 503


def test_pull_poll_background_and_status(app, monkeypatch):
    """有 poller → POST 立即返回 running=true；后台跑完 /status 返回报告。"""
    fast, token = app

    class FakePoller:
        async def run_once(self):
            await asyncio.sleep(0.01)
            return {"projects": 1, "prs": 2, "new": 1, "skipped": 1, "errors": []}

    fast.state.poller = FakePoller()
    fast.state.poll_running = False
    fast.state.poll_last = None
    fast.state.poll_error = None
    fast.state.poll_run_task = None
    with _client(fast, token) as c:
        r = c.post("/api/pulls/poll")
        assert r.status_code == 200, r.text
        assert r.json()["running"] is True
        for _ in range(50):
            s = c.get("/api/pulls/poll/status").json()
            if not s["running"]:
                break
            time.sleep(0.02)
        assert s["running"] is False
        assert s["report"]["new"] == 1
        assert s["report"]["skipped"] == 1

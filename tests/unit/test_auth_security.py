"""阶段 D 开放安全加固测试：登录限速 + 算术验证码 + JWT refresh 轮换 + 可撤销会话 + 邮箱验证。

离线：临时 SQLite + httpx TestClient，`LoginGuard` 由测试显式挂到 `app.state.login_guard`
（每次独立 new 一个可复位实例，避免影响既有测试）。覆盖：
- IP 限速：窗口内超阈值 → 429；复位后恢复。
- 验证码：连续失败达阈值后未带验证码 → 400/captcha_required；取题答对通过、错答拦截、一次性。
- refresh：登录→refresh 轮换新 access+新 refresh；旧 refresh 复用 → 401；logout 后 refresh → 401。
- 可撤销：logout 后带 sid 的 access 访问 /me → 401；无 sid 旧 token 仍可用（迁移兼容）。
- 邮箱验证：注册带 email（monkeypatch 捕获 token,禁网络）→ verify 置 email_verified=True。

注意 Windows 子进程解码：仓库既有约定显式 encoding="utf-8"（见 memory）。
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.security_guard import LoginGuard
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.seed import ensure_member_role, ensure_workspace_backfill
from tests.unit.helpers import make_admin_app

ADMIN_PW = "hunter2"


async def _extra_seed(session) -> None:
    """补 member 角色 + 默认工作区（模拟主启动时序），注册/登录闭环所需。"""
    await ensure_member_role(session)
    await ensure_workspace_backfill(session)


def _login(c: TestClient, username: str, password: str, **extra) -> dict:
    body = {"username": username, "password": password}
    body.update(extra)
    return c.post("/api/auth/login", json=body)


def _register(c: TestClient, username: str, *, password: str = "pass1234",
              email: str = "") -> dict:
    body = {"username": username, "password": password}
    if email:
        body["email"] = email
    r = c.post("/api/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _captcha_answer(c: TestClient) -> tuple[str, str]:
    """取一道算术验证码，返回 (captcha_id, 计算出的答案字符串)。"""
    r = c.post("/api/auth/captcha")
    assert r.status_code == 200, r.text
    data = r.json()
    nums = [int(x) for x in re.findall(r"\d+", data["prompt"])]
    return data["captcha_id"], str(sum(nums))


async def _app(tmp_path, **guard_kwargs) -> FastAPI:
    guard = LoginGuard(**guard_kwargs)
    fast, _token, _admin, _engine = await make_admin_app(
        tmp_path, db_name="authsec.db", seed=_extra_seed,
    )
    fast.state.login_guard = guard
    return fast


# ---------------- 登录限速 ----------------

@pytest.mark.asyncio
async def test_rate_limit_429_then_reset(tmp_path):
    fast = await _app(tmp_path, login_rate_attempts=3, login_rate_window_seconds=60)
    with TestClient(fast) as c:
        for _ in range(3):
            r = _login(c, "admin", "wrong")
            assert r.status_code in (400, 401)  # 前 3 次在窗口内被允许（都记尝试）
        r = _login(c, "admin", "wrong")
        assert r.status_code == 429  # 第 4 次超限
        assert "频繁" in r.json()["detail"]
        # 复位后恢复
        fast.state.login_guard.reset()
        r = _login(c, "admin", ADMIN_PW)
        assert r.status_code == 200


# ---------------- 验证码 ----------------

@pytest.mark.asyncio
async def test_captcha_required_after_failures_and_answer(tmp_path):
    fast = await _app(tmp_path, captcha_threshold_attempts=2, login_rate_attempts=100)
    with TestClient(fast) as c:
        # 前 2 次错口令 → 401，第 2 次响应头标记需验证码
        r1 = _login(c, "admin", "wrong")
        assert r1.status_code == 401
        r2 = _login(c, "admin", "wrong")
        assert r2.status_code == 401
        assert "X-Captcha-Required" in r2.headers
        # 未带验证码 → 400（X-Captcha-Required 头即信号；body 仅 detail）
        r3 = _login(c, "admin", "wrong")
        assert r3.status_code == 400
        assert "X-Captcha-Required" in r3.headers
        r4 = _login(c, "admin", "wrong")
        assert r4.status_code == 400 and "X-Captcha-Required" in r4.headers
        # 取题答错 → 400（校验通过但口令对上也过不去，这里用错口令验闸门）；答对 + 正确口令 → 200
        cid, wrong_ans = _captcha_answer(c)
        r_bad = _login(c, "admin", ADMIN_PW, captcha_id=cid, captcha_answer=str(int(wrong_ans) + 1))
        assert r_bad.status_code == 400
        cid, ans = _captcha_answer(c)
        r_ok = _login(c, "admin", ADMIN_PW, captcha_id=cid, captcha_answer=ans)
        assert r_ok.status_code == 200, r_ok.text
        assert r_ok.json()["access_token"]
        assert r_ok.json()["refresh_token"]
        # 验证码一次性：同一 id 不可再用（成功登录已把会话失败计数清零，故手动重灌失败再验）
        guard = fast.state.login_guard
        guard.record_failure("testclient", "admin")
        guard.record_failure("testclient", "admin")
        cid2, ans2 = _captcha_answer(c)
        r_consume = _login(c, "admin", "wrong", captcha_id=cid2, captcha_answer=ans2)
        assert r_consume.status_code == 401  # captcha 通过但口令错
        r_reuse = _login(c, "admin", "wrong", captcha_id=cid2, captcha_answer=ans2)
        assert r_reuse.status_code == 400  # 同一 id 已被一次性消耗


# ---------------- refresh 轮换 + 可撤销 ----------------

@pytest.mark.asyncio
async def test_refresh_rotation_and_revocation(tmp_path):
    fast = await _app(tmp_path)
    with TestClient(fast) as c:
        login = _login(c, "admin", ADMIN_PW)
        assert login.status_code == 200, login.text
        tok = login.json()
        first_refresh = tok["refresh_token"]
        # refresh 轮换：新 access（可再用）+ 新 refresh（随机段已轮换）
        r = c.post("/api/auth/refresh", json={"refresh_token": first_refresh})
        assert r.status_code == 200, r.text
        new_access, new_refresh = r.json()["access_token"], r.json()["refresh_token"]
        assert new_refresh != first_refresh  # refresh 秘密已轮换（旧 refresh 复用即作废）
        # 旧 refresh 复用 → 401（已轮换作废）
        assert c.post("/api/auth/refresh", json={"refresh_token": first_refresh}).status_code == 401
        # 新 access 可访问受保护端点
        me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "admin"
        # logout → 会话撤销，其 access 即刻失效、refresh 也失效
        logout = c.post("/api/auth/logout", json={"refresh_token": new_refresh})
        assert logout.status_code == 200
        me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {new_access}"})
        assert me.status_code == 401  # logout 已撤销该会话的 access
        assert c.post("/api/auth/refresh", json={"refresh_token": new_refresh}).status_code == 401


@pytest.mark.asyncio
async def test_legacy_sidless_token_still_works(tmp_path):
    """无 sid 的旧 token（存量/测试签发）仍走 decode+查 user，迁移期兼容。"""
    from codereview_ai.api.auth import issue_token
    fast, _token, admin, _engine = await make_admin_app(
        tmp_path, db_name="legacy.db", seed=_extra_seed,
    )
    legacy = issue_token(fast.state.settings.secret_key, sub=str(admin.id))
    with TestClient(fast) as c:
        me = c.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"})
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "admin"


# ---------------- 邮箱验证 ----------------

@pytest.mark.asyncio
async def test_email_verify_flag(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_send(settings, *, to_email, username, token, base_url=""):
        captured["token"] = token
        captured["to_email"] = to_email
        captured["username"] = username
        return True

    monkeypatch.setattr("codereview_ai.api.auth.send_verification_email", fake_send)
    fast = await _app(tmp_path)
    with TestClient(fast) as c:
        reg = _register(c, "erik", email="erik@example.com")
        assert reg["user"]["email"] == "erik@example.com"
        assert reg["user"]["email_verified"] is False
        assert captured["to_email"] == "erik@example.com"
        token = captured["token"]
        # 验证前 /auth/me? —— 用 member 登录看 email_verified 尚未置位
        # 直接验证：GET /auth/verify-email?token=... → ok + email_verified=True
        r = c.get("/api/auth/verify-email", params={"token": token})
        assert r.status_code == 200, r.text
        assert r.json()["email_verified"] is True
        # DB 持久化
        from sqlalchemy import select

        from codereview_ai.storage.models import User

        async def _check():
            async with session_factory(fast.state.engine)() as s:
                row = await s.execute(select(User.email_verified).where(User.username == "erik"))
                return row.scalar_one()

        assert await _check() is True
        # 非法/过期 token → 400
        assert c.get("/api/auth/verify-email", params={"token": "garbage"}).status_code == 400


@pytest.mark.asyncio
async def test_register_without_smtp_is_graceful(tmp_path):
    """SMTP 未配置时注册带 email 不报错、不尝试外发、email_verified=False。"""
    fast = await _app(tmp_path)
    with TestClient(fast) as c:
        reg = _register(c, "finn", email="finn@example.com")
        assert reg["user"]["email"] == "finn@example.com"
        assert reg["user"]["email_verified"] is False
        # 登录仍正常
        assert _login(c, "finn", "pass1234").status_code == 200

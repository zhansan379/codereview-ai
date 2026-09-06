"""后台鉴权测试（DESIGN §14.3，F5.1）：JWT 登录 + get_current_user。

用最小 FastAPI 应用挂真实 auth 路由与一个受保护示例端点，注入 `app.state.settings`，全程离线。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.api.deps import get_current_user
from codereview_ai.config import Settings


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


def _settings(**kw) -> Settings:
    opts = dict(secret_key="s", webhook_secret="w", encryption_key=_fernet_key(),
                admin_password="hunter2")
    opts.update(kw)
    return Settings(**opts)


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(auth_router, prefix="/api")

    @app.get("/api/secure")
    async def secure(user: str = Depends(get_current_user)) -> dict[str, str]:
        return {"user": user}

    return app


def test_login_wrong_password_401():
    with TestClient(_app(_settings())) as client:
        r = client.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401


def test_login_success_issues_jwt():
    s = _settings()
    with TestClient(_app(s)) as client:
        r = client.post("/api/auth/login", json={"password": "hunter2"})
        assert r.status_code == 200
        body = r.json()
        assert body["token_type"] == "bearer"
        payload = jwt.decode(body["access_token"], s.secret_key, algorithms=["HS256"])
        assert payload["sub"] == "admin"


def test_issue_and_decode_token():
    s = _settings()
    payload = jwt.decode(issue_token(s.secret_key), s.secret_key, algorithms=["HS256"])
    assert payload["sub"] == "admin"
    assert payload["exp"] > int(datetime.now(UTC).timestamp())


def test_secure_requires_bearer():
    with TestClient(_app(_settings())) as client:
        assert client.get("/api/secure").status_code == 401
        assert client.get("/api/secure",
                          headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_secure_accepts_valid_token():
    s = _settings()
    token = issue_token(s.secret_key)
    with TestClient(_app(s)) as client:
        r = client.get("/api/secure", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json() == {"user": "admin"}


def test_expired_token_rejected():
    s = _settings()
    expired = jwt.encode(
        {"sub": "admin", "exp": datetime.now(UTC) - timedelta(hours=1)},
        s.secret_key,
        algorithm="HS256",
    )
    with TestClient(_app(s)) as client:
        assert client.get("/api/secure",
                          headers={"Authorization": f"Bearer {expired}"}).status_code == 401

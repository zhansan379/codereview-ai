"""api/webhook 测试：签名校验 → 投递；无 secret / 坏签名 / 空 body。

用独立 FastAPI app 手动注入 state（不经 create_app，避免 Settings fail-fast）。
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.webhook import router

SECRET = "wh-secret"


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def enqueue(self, provider: str, raw: bytes) -> None:
        self.calls.append((provider, raw))


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router)
    settings = type("S", (), {"webhook_secret": SECRET})()
    enq = _FakeEnqueuer()
    app.state.settings = settings
    app.state.enqueuer = enq
    return TestClient(app), enq


def _gitlab_headers(token: str) -> dict[str, str]:
    return {"X-Gitlab-Token": token, "Content-Type": "application/json", "X-Forwarded-For": "9.9.9.9"}  # noqa: E501


def test_valid_gitlab_event_is_enqueued_202(client):
    tc, enq = client
    body = b'{"object_kind": "merge_request", "object_attributes": {"iid": 5}}'
    r = tc.post("/webhook", content=body, headers=_gitlab_headers(SECRET))
    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    assert len(enq.calls) == 1
    assert enq.calls[0][0] == "gitlab"
    assert enq.calls[0][1] == body  # 投递原始 bytes，未重序列化


def test_bad_signature_returns_401(client):
    tc, enq = client
    r = tc.post("/webhook", content=b"{}", headers=_gitlab_headers("wrong"))
    assert r.status_code == 401
    assert enq.calls == []


def test_missing_secret_on_state_is_500(client):
    tc, enq = client
    enq.calls.clear()
    # 手动把 secret 置空
    tc.app.state.settings = type("S", (), {"webhook_secret": ""})()
    r = tc.post("/webhook", content=b"{}", headers=_gitlab_headers(SECRET))
    assert r.status_code == 500


def test_empty_body_returns_400(client):
    tc, enq = client
    r = tc.post("/webhook", content=b"", headers=_gitlab_headers(SECRET))
    assert r.status_code == 400


def test_github_signature_path(client):
    tc, enq = client
    body = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = tc.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig})  # noqa: E501
    assert r.status_code == 202
    assert enq.calls[0][0] == "github"


def test_no_enqueuer_is_503(client):
    tc, enq = client
    del tc.app.state.enqueuer
    r = tc.post("/webhook", content=b"{}", headers=_gitlab_headers(SECRET))
    assert r.status_code == 503

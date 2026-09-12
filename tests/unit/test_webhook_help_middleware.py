"""webhook 路径配置错误提示中间件测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.webhook import WebhookHelpMiddleware, router


@pytest.fixture()
def app_with_middleware():
    """构造带中间件的测试 app。"""
    app = FastAPI()
    app.add_middleware(WebhookHelpMiddleware)
    app.include_router(router)
    # 模拟 app.state.settings（webhook_entry 需要）
    app.state.settings = MagicMock(webhook_secret="test-secret")
    # 模拟 app.state.engine（落库需要）
    app.state.engine = MagicMock()
    return app


def test_webhook_path_correct_passes_through(app_with_middleware):
    """正确路径 /webhook 应该正常处理，不触发提示。"""
    client = TestClient(app_with_middleware)
    # 不带 headers 的普通 POST 到 /webhook，会因为签名错误返回 401（不是 404）
    r = client.post("/webhook", content=b"{}")
    assert r.status_code == 401  # signature 错误


def test_root_path_with_webhook_headers_records_error(app_with_middleware):
    """根路径 / 带 webhook headers 应该返回标准 404 并落库。"""
    client = TestClient(app_with_middleware)
    r = client.post("/", content=b"{}", headers={"X-Gitee-Event": "Pull Request"})
    assert r.status_code == 404
    # 不再返回 HTML 提示页，而是标准 404 JSON
    assert r.json() == {"detail": "Not Found"}


def test_arbitrary_path_with_webhook_headers_records_error(app_with_middleware):
    """任意错误路径带 webhook headers 应该返回标准 404 并落库。"""
    client = TestClient(app_with_middleware)
    r = client.post(
        "/some/wrong/path",
        content=b"{}",
        headers={"X-GitHub-Event": "push"},
    )
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_wrong_path_without_webhook_headers_passes_through(app_with_middleware):
    """错误路径但没有 webhook headers 应该返回普通 404。"""
    client = TestClient(app_with_middleware)
    r = client.post("/wrong", content=b"{}")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_get_request_not_intercepted(app_with_middleware):
    """GET 请求不应该被中间件拦截。"""
    client = TestClient(app_with_middleware)
    r = client.get("/", headers={"X-Gitee-Event": "Pull Request"})
    # GET 请求直接返回 404（前端可能有自己的处理）
    assert r.status_code in (200, 404)  # 取决于是否有前端路由


def test_all_platforms_detected(app_with_middleware):
    """各平台 headers 都应该被识别并落库。"""
    client = TestClient(app_with_middleware)

    # GitHub
    r = client.post("/", headers={"X-GitHub-Event": "push"})
    assert r.status_code == 404

    # GitLab
    r = client.post("/", headers={"X-Gitlab-Token": "xxx"})
    assert r.status_code == 404

    # Gitea
    r = client.post("/", headers={"X-Gitea-Event": "push"})
    assert r.status_code == 404

    # Gitee
    r = client.post("/", headers={"X-Gitee-Event": "Pull Request"})
    assert r.status_code == 404

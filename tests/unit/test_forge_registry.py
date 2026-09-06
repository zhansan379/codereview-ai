"""forges/registry 测试：按配置注册平台、按 provider 构造适配器。

构造适配器不触网络（HTTP 客户端注入、无调用），纯离线。
"""

from __future__ import annotations

import httpx

from codereview_ai.config import Settings
from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.gitlab import GitLabForge
from codereview_ai.forges.registry import build_adapter, registered_providers
from codereview_ai.forges.signatures import GITHUB, GITLAB

DUMMY = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def _settings(**kw) -> Settings:
    """构造带默认密钥的 Settings，供 registry 读取平台配置。"""
    opts = dict(
        secret_key="s", webhook_secret="w", encryption_key="ZGVmZg==A", admin_password="a",
    )
    # Fermet 格式：44 位 urlsafe base64 结尾驼 =。给出合法占位。
    from cryptography.fernet import Fernet
    opts["encryption_key"] = Fernet.generate_key().decode()
    opts.update(kw)
    return Settings(**opts)


def test_no_configured_provider_returns_empty():
    s = _settings()
    assert registered_providers(s) == []


def test_gitlab_only_when_token():
    s = _settings(gitlab_url="https://gl.example.com", gitlab_token="t")
    assert registered_providers(s) == [GITLAB]


def test_both_platforms_when_tokens():
    s = _settings(gitlab_url="u", gitlab_token="t", github_token="g")
    assert set(registered_providers(s)) == {GITLAB, GITHUB}


def test_build_adapter_selects_correct_type():
    s = _settings(gitlab_url="https://gl.example.com", gitlab_token="t")
    f = build_adapter(GITLAB, s, DUMMY)
    assert isinstance(f, GitLabForge)


def test_build_adapter_unconfigured_returns_none():
    s = _settings()
    assert build_adapter(GITHUB, s, DUMMY) is None


def test_build_adapter_requires_token():
    s = _settings(github_url="https://api.github.com")  # 有 url 无 token
    assert build_adapter(GITHUB, s, DUMMY) is None


def test_github_adapter_with_config():
    s = _settings(github_token="g")
    f = build_adapter(GITHUB, s, DUMMY)
    assert isinstance(f, GitHubForge)

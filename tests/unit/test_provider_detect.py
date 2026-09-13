"""仓库链接 → 平台自动识别测试（新增项目免选平台）：host 映射 + 特征路径探测。"""

from __future__ import annotations

import asyncio

import httpx

from codereview_ai.api.admin.forges import detect_provider_by_probe
from codereview_ai.forges.base import provider_from_url_host

# ── provider_from_url_host（纯 host 映射，不发网络请求） ─────────────────────────

def test_public_hosts_map_directly():
    assert provider_from_url_host("https://github.com/acme/widgets") == "github"
    assert provider_from_url_host("https://gitlab.com/a/b/-/merge_requests/9") == "gitlab"
    assert provider_from_url_host("https://gitee.com/acme/widgets") == "gitee"


def test_selfhosted_naming_keywords():
    assert provider_from_url_host("https://gitlab.corp.com/acme/widgets") == "gitlab"
    assert provider_from_url_host("https://gitea.example.cn/acme/widgets") == "gitea"
    assert provider_from_url_host("https://gitee.internal/acme/widgets") == "gitee"
    assert provider_from_url_host("https://github.enterprise.io/acme/widgets") == "github"


def test_unknown_host_returns_empty():
    assert provider_from_url_host("https://git.corp.com/acme/widgets") == ""
    assert provider_from_url_host("https://scm.example.com/acme/widgets") == ""


def test_host_normalization_strips_userinfo_port_case():
    assert provider_from_url_host("https://user:pass@GitHub.COM:8443/acme/widgets") == "github"
    assert provider_from_url_host("not-a-url") == ""
    assert provider_from_url_host("") == ""


# ── detect_provider_by_probe（特征 API 路径，MockTransport 离线） ─────────────────

def _route(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: responses.get(request.url.path, httpx.Response(404)))


def test_probe_detects_gitea():
    transport = _route({"/api/v1/version": httpx.Response(200, json={"version": "1.21"})})
    got = asyncio.run(detect_provider_by_probe("https://git.corp.com/acme/widgets", transport))
    assert got == "gitea"


def test_probe_detects_gitlab_via_401():
    # 私有 GitLab 对未认证的特征路径回 401 JSON——路径存在即平台在
    transport = _route({"/api/v4/version": httpx.Response(401, json={"message": "401"})})
    got = asyncio.run(detect_provider_by_probe("https://git.corp.com/acme/widgets", transport))
    assert got == "gitlab"


def test_probe_detects_ghe_via_v3():
    transport = _route({"/api/v3": httpx.Response(401, json={"message": "Requires auth"})})
    got = asyncio.run(detect_provider_by_probe("https://ghe.corp.com/acme/widgets", transport))
    assert got == "github"


def test_probe_html_catchall_is_unrecognized():
    def _home_html(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>home</html>",
                              headers={"content-type": "text/html"})

    html = httpx.MockTransport(_home_html)
    got = asyncio.run(detect_provider_by_probe("https://scm.corp.com/acme/widgets", html))
    assert got == ""


def test_probe_all_404_is_unrecognized():
    got = asyncio.run(detect_provider_by_probe("https://scm.corp.com/acme/widgets", _route({})))
    assert got == ""


def test_probe_invalid_url_returns_empty():
    assert asyncio.run(detect_provider_by_probe("not-a-url")) == ""

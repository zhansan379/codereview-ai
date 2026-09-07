"""forges/scopes 能力探测测试：GitHub scope 与 GitLab 读探针的 ok/missing/unknown 三分。

用 httpx.MockTransport 注入假 HTTP，全程离线。
"""

from __future__ import annotations

import httpx

from codereview_ai.forges.scopes import probe_capabilities


def _probe(provider: str, handler) -> list:
    transport = httpx.MockTransport(handler)
    return __import__("asyncio").run(probe_capabilities(
        provider, "https://base.example", "tok", http=httpx.AsyncClient(transport=transport, timeout=5),
    ))


def _caps(provider: str, handler) -> dict[str, str]:
    return {c.name: c.status for c in _probe(provider, handler)}


# ── GitHub：X-OAuth-Scopes 权威判定 ────────────────────────────────────────


def _gh(scopes: str, status: int = 200) -> dict[str, str]:
    def handler(r: httpx.Request) -> httpx.Response:
        assert "Authorization" in r.headers  # Bearer token 已带
        headers = {"X-OAuth-Scopes": scopes} if scopes else {}
        return httpx.Response(status, headers=headers, json={})
    return _caps("github", handler)


def test_github_full_scopes_all_ok():
    # repo 覆盖 read_pull/post_comment/commit_status
    caps = _gh("repo")
    assert caps == {"connect": "ok", "read_pull": "ok", "post_comment": "ok", "commit_status": "ok"}


def test_github_public_repo_and_repo_status():
    # public_repo 只够读；repo:status 只够写状态
    caps = _gh("public_repo,repo:status")
    assert caps["read_pull"] == "ok"
    assert caps["post_comment"] == "missing"
    assert caps["commit_status"] == "ok"


def test_github_no_relevant_scope_all_missing_with_detail():
    caps = _probe("github", lambda r: httpx.Response(
        200, headers={"X-OAuth-Scopes": "read:user"}, json={},
    ))
    by = {c.name: c for c in caps}
    assert by["read_pull"].status == "missing"
    assert "repo" in by["read_pull"].detail
    assert by["post_comment"].status == "missing"


def test_github_auth_failure_connect_missing():
    caps = _gh("", status=401)
    assert caps["connect"] == "missing"
    assert caps["read_pull"] == "unknown"  # 认证未过，其余无法判定


def test_github_missing_scope_header_is_unknown():
    # fine-grained PAT 不返回 X-OAuth-Scopes → 诚实标 unknown，不猜
    caps = _gh("")
    assert caps["connect"] == "ok"
    assert caps["read_pull"] == "unknown"
    assert caps["post_comment"] == "unknown"


# ── GitLab：/user 判 connect，/projects 判 read_pull，写能力 unknown ──────────


def test_gitlab_all_ok():
    def handler(r: httpx.Request) -> httpx.Response:
        if "/user" in str(r.url):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[])  # /projects 读探针 2xx
    caps = _caps("gitlab", handler)
    assert caps["connect"] == "ok"
    assert caps["read_pull"] == "ok"
    # 写能力不做写探针 → unknown
    assert caps["post_comment"] == "unknown"
    assert caps["commit_status"] == "unknown"


def test_gitlab_auth_failure_connect_missing():
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})
    caps = _caps("gitlab", handler)
    assert caps["connect"] == "missing"
    assert caps["read_pull"] == "unknown"


def test_gitlab_projects_forbidden_read_missing():
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(str(r.url))
        if "/user" in str(r.url):
            return httpx.Response(200, json={})
        return httpx.Response(403, json={})

    caps = _probe("gitlab", handler)
    by = {c.name: c for c in caps}
    assert _caps("gitlab", handler) == {"connect": "ok", "read_pull": "missing",
                                        "post_comment": "unknown", "commit_status": "unknown"}
    assert "read_api / api" in by["read_pull"].detail


def test_gitlab_projects_server_error_is_unknown():
    def handler(r: httpx.Request) -> httpx.Response:
        if "/user" in str(r.url):
            return httpx.Response(200, json={})
        return httpx.Response(500, json={})

    caps = _caps("gitlab", handler)
    assert caps["read_pull"] == "unknown"
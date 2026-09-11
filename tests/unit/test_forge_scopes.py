"""forges/scopes 能力探测测试：GitHub scope 与 GitLab 读探针的 ok/missing/unknown 三分。

用 httpx.MockTransport 注入假 HTTP，全程离线。
"""

from __future__ import annotations

import httpx

from codereview_ai.forges.scopes import probe_capabilities


def _probe(provider: str, handler, token: str = "tok") -> list:
    transport = httpx.MockTransport(handler)
    return __import__("asyncio").run(probe_capabilities(
        provider, "https://base.example", token, http=httpx.AsyncClient(transport=transport, timeout=5),
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
    # 无前缀可识别（`tok`）→ 诚实标 unknown，不猜
    caps = _gh("")
    assert caps["connect"] == "ok"
    assert caps["read_pull"] == "unknown"
    assert caps["post_comment"] == "unknown"


def test_github_fine_grained_read_probe_ok():
    # github_pat_ 前缀识别为 fine-grained；读/写探针全过 → 各能力判定
    def handler(r: httpx.Request) -> httpx.Response:
        if "/user/repos" in str(r.url):
            return httpx.Response(200, json=[{"id": 1, "full_name": "owner/repo"}])
        return httpx.Response(200, json={})  # /user 无 X-OAuth-Scopes
    caps = _probe("github", handler, token="github_pat_abc")
    by = {c.name: c for c in caps}
    assert by["connect"].status == "ok"
    assert "Fine-grained" in by["connect"].detail
    assert by["read_pull"].status == "ok"
    assert "读探针" in by["read_pull"].detail
    assert by["post_comment"].status == "ok"
    assert by["commit_status"].status == "ok"


def _fg_handler(
    repos: list, issue: int = 404, review: int = 404, status: int = 422,
) -> httpx.Request:
    """fine-grained 场景 handler：/user 无 scope 头；写探针目标是不存在资源 → 404/422=有权。"""

    def handler(r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        if "/user/repos" in url:
            return httpx.Response(200, json=repos)
        if "/issues/0/comments" in url:
            return httpx.Response(issue, json={})
        if "/pulls/0/reviews" in url:
            return httpx.Response(review, json={})
        if "/statuses/" in url:
            return httpx.Response(status, json={})
        return httpx.Response(200, json={})  # /user
    return handler


def test_github_fine_grained_write_probe_all_ok():
    # 有可读仓库；写探针全部 404/422（资源不存在但有权限）→ 评论/状态均 ok
    caps = _probe("github", _fg_handler([{"full_name": "owner/repo"}]), token="github_pat_abc")
    by = {c.name: c for c in caps}
    assert by["post_comment"].status == "ok"
    assert by["commit_status"].status == "ok"
    assert by["read_pull"].status == "ok"


def test_github_fine_grained_write_probe_missing_lists_perms():
    # 两个评论端点都 403 → post_comment=missing，detail 列出缺的权限；status 403 → missing
    caps = _probe(
        "github", _fg_handler([{"full_name": "owner/repo"}], issue=403, review=403, status=403),
        token="github_pat_abc",
    )
    by = {c.name: c for c in caps}
    assert by["post_comment"].status == "missing"
    assert "Issues: write" in by["post_comment"].detail
    assert "Pull requests: write" in by["post_comment"].detail
    assert by["commit_status"].status == "missing"
    assert "Commit statuses: write" in by["commit_status"].detail


def test_github_fine_grained_write_probe_partial():
    # 只有行级缺权 → detail 只列 Pull requests: write；状态探针异常 → unknown
    caps = _probe(
        "github", _fg_handler([{"full_name": "owner/repo"}], issue=404, review=403, status=500),
        token="github_pat_abc",
    )
    by = {c.name: c for c in caps}
    assert by["post_comment"].status == "missing"
    assert "Pull requests: write" in by["post_comment"].detail
    assert "Issues: write" not in by["post_comment"].detail
    assert by["commit_status"].status == "unknown"


def test_github_fine_grained_write_probe_multi_repo():
    # 首个仓库（可能不在 token 授权范围）403，第二仓库有权限 → 应判 ok，不误报缺权限
    def handler(r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        if "/user/repos" in url:
            return httpx.Response(200, json=[{"full_name": "a/one"}, {"full_name": "b/two"}])
        if "/repos/a/one/" in url:
            return httpx.Response(403, json={})
        if "/repos/b/two/" in url:
            if "/statuses/" in url:
                return httpx.Response(422, json={})
            return httpx.Response(404, json={})
        return httpx.Response(200, json={})
    caps = _probe("github", handler, token="github_pat_abc")
    by = {c.name: c for c in caps}
    assert by["read_pull"].status == "ok"
    assert by["post_comment"].status == "ok"
    assert by["commit_status"].status == "ok"


def test_github_fine_grained_deny_surfaces_github_hint():
    # 403 响应带 X-Accepted-GitHub-Permissions → detail 直接展示 GitHub 的权威原因
    def handler(r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        if "/user/repos" in url:
            return httpx.Response(200, json=[{"full_name": "owner/repo"}])
        if "/statuses/" in url:
            return httpx.Response(
                403, headers={"X-Accepted-GitHub-Permissions": "commit_statuses: write"}, json={},
            )
        if "/issues/0/comments" in url or "/pulls/0/reviews" in url:
            return httpx.Response(
                403, json={"message": "Resource not accessible by fine-grained PAT"},
            )
        return httpx.Response(200, json={})
    caps = _probe("github", handler, token="github_pat_abc")
    by = {c.name: c for c in caps}
    assert "commit_statuses: write" in by["commit_status"].detail
    assert "Resource not accessible" in by["post_comment"].detail


def test_github_fine_grained_read_probe_empty_unknown():
    # 未授权任何可读仓库 → 无写探针目标，读/写均 unknown（诚实）
    caps = _probe("github", _fg_handler([]), token="github_pat_abc")
    assert {c.name: c.status for c in caps} == {
        "connect": "ok", "read_pull": "unknown", "post_comment": "unknown",
        "commit_status": "unknown",
    }


def test_github_fine_grained_read_probe_forbidden_missing():
    # 读仓库列表被拒 → read_pull=missing，且没有可写探针目标
    def handler(r: httpx.Request) -> httpx.Response:
        if "/user/repos" in str(r.url):
            return httpx.Response(403, json={})
        return httpx.Response(200, json={})
    caps = _probe("github", handler, token="github_pat_abc")
    by = {c.name: c for c in caps}
    assert by["read_pull"].status == "missing"
    assert "403" in by["read_pull"].detail
    assert by["post_comment"].status == "unknown"


def test_github_app_token_detail_specific():
    # ghs_（GitHub App 安装令牌）同样不返回 scope 头 → 提示按类型精确化
    caps = _probe("github", lambda r: httpx.Response(200, json={}), token="ghs_xyz")
    by = {c.name: c for c in caps}
    assert by["connect"].status == "ok"
    assert "github_app" in by["connect"].detail
    assert "GitHub App 安装令牌" in by["read_pull"].detail


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
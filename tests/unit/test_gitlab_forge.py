"""forges/gitlab 测试：payload 解析、changes/fetch 逆转换、空数组重试、评论回写。

用 httpx.MockTransport 注入假 HTTP，全程离线，不碰网络。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from codereview_ai.domain.models import ChangeType, PullRequest
from codereview_ai.forges import gitlab as gl_mod
from codereview_ai.forges.gitlab import (
    GitLabForge,
    _to_file_diff,
    parse_merge_request_payload,
    parse_push_event_payload,
)
from codereview_ai.forges.signatures import GITLAB

API_BASE = "https://gitlab.example.com"
TOKEN = "glpat-test"


def _sample_mr_payload() -> dict:
    return {
        "object_kind": "merge_request",
        "user": {"username": "alice"},
        "project": {"id": 7, "path_with_namespace": "acme/widgets"},
        "object_attributes": {
            "action": "update",
            "iid": 42,
            "target_project_id": 7,
            "title": "feat: add paging",
            "source_branch": "feat-paging",
            "target_branch": "main",
            "last_commit": {"id": "abc123"},
            "url": "https://gitlab.example.com/acme/widgets/-/merge_requests/42",
        },
    }


def _pr() -> PullRequest:
    return parse_merge_request_payload(_sample_mr_payload())  # type: ignore[return-value]


def _forge(handler: Callable[[httpx.Request], httpx.Response]) -> GitLabForge:
    transport = httpx.MockTransport(handler)
    return GitLabForge(API_BASE, TOKEN, httpx.AsyncClient(transport=transport, timeout=5))


# ── payload 解析 ────────────────────────────────────────────────────────


def test_parse_merge_request_maps_fields():
    pr = _pr()
    assert pr.provider == GITLAB
    assert pr.repo_id == "7"
    assert pr.repo_full_name == "acme/widgets"
    assert pr.pr_number == 42
    assert pr.title == "feat: add paging"
    assert pr.source_branch == "feat-paging"
    assert pr.target_branch == "main"
    assert pr.head_sha == "abc123"
    assert pr.author == "alice"


def test_parse_non_merge_request_is_none():
    assert parse_merge_request_payload({"object_kind": "push"}) is None


def test_parse_missing_iid_is_none():
    payload = _sample_mr_payload()
    payload["object_attributes"].pop("iid")
    assert parse_merge_request_payload(payload) is None


def test_parse_mr_non_dict_project_and_last_commit():
    payload = _sample_mr_payload()
    payload["project"] = "oops"  # 非 dict → 兜底为空 dict
    payload["object_attributes"]["last_commit"] = None  # 非 dict/None → 兜底为空 dict
    pr = parse_merge_request_payload(payload)
    assert pr.repo_id == "7"  # target_project_id 仍在
    assert pr.head_sha == ""


def test_parse_push_missing_repo_is_none():
    payload = {"object_kind": "push", "project": {"id": None}}
    assert parse_push_event_payload(payload) is None


def test_should_review_gates_actions():
    f = _forge(lambda r: httpx.Response(500))
    assert f.should_review("open")
    assert f.should_review("update")
    assert not f.should_review("close")
    assert not f.should_review("merge")


# ── changes → FileDiff ──────────────────────────────────────────────────


def test_to_file_diff_new_flag():
    item = {
        "old_path": "foo.py",
        "new_path": "foo.py",
        "new_file": True,
        "deleted_file": False,
        "renamed_file": False,
        "diff": "--- a/foo.py\n+++ b/foo.py\n@@ -0,0 +1,2 @@\n+line1\n+line2\n",
    }
    d = _to_file_diff(item)
    assert d.change_type == ChangeType.NEW_FILE
    assert d.additions == 2
    assert d.deletions == 0
    assert d.new_file_content == "line1\nline2"


def test_to_file_diff_deleted_and_renamed():
    deleted = _to_file_diff({"old_path": "a.py", "new_path": "a.py", "deleted_file": True, "diff": "-gone\n"})  # noqa: E501
    assert deleted.change_type == ChangeType.DELETED_FILE
    renamed = _to_file_diff({"old_path": "old.py", "new_path": "new.py", "renamed_file": True, "diff": ""})  # noqa: E501
    assert renamed.change_type == ChangeType.RENAMED_FILE


def test_to_file_diff_modified_restores_new_content():
    item = {
        "old_path": "a.py",
        "new_path": "a.py",
        "new_file": False,
        "deleted_file": False,
        "renamed_file": False,
        "diff": "--- a.py\n+++ b.py\n@@ -1 +1,2 @@\n ctx\n+added\n",
    }
    d = _to_file_diff(item)
    assert d.change_type == ChangeType.MODIFIED
    # 修改文件也要能还原新侧全文（覆盖集/复用判定依据），不再是空串
    assert d.new_file_content == "ctx\nadded"


# ── fetch_files 拉取 + 空数组重试 ──────────────────────────────────────


def test_fetch_files_converts_changes():
    handler = lambda r: httpx.Response(200, json={  # noqa: E731
        "changes": [
            {
                "old_path": "a.py",
                "new_path": "a.py",
                "diff": "---\n+++\n@@ -1 +1,2 @@\n context\n+added\n",
            }
        ]
    })
    diffs = asyncio.run(_forge(handler).fetch_files(_pr()))
    assert len(diffs) == 1
    assert diffs[0].new_path == "a.py"
    assert diffs[0].additions == 1


def test_fetch_files_retries_on_empty_then_succeeds():
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:  # 前两次空，第三次有数据
            return httpx.Response(200, json={"changes": []})
        return httpx.Response(200, json={"changes": [{"old_path": "x", "new_path": "x", "diff": "+y\n"}]})  # noqa: E501

    diffs = asyncio.run(_forge(handler).fetch_files(_pr()))
    assert len(calls) == 3
    assert len(diffs) == 1


def test_fetch_files_exhausts_retries_returns_empty(monkeypatch):
    # 缩小退避参数，快速走完全部尝试后仍空 → 返回 []
    monkeypatch.setattr(gl_mod, "_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(gl_mod, "_RETRY_DELAY_0", 0.001)
    handler = lambda r: httpx.Response(200, json={"changes": []})  # noqa: E731
    assert asyncio.run(_forge(handler).fetch_files(_pr())) == []


# ── fetch_pull_request 补 diff_refs ─────────────────────────────────────


def test_fetch_pull_request_populates_diff_refs():
    def handler(r: httpx.Request) -> httpx.Response:
        assert API_BASE in str(r.url)
        return httpx.Response(200, json={"diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}})  # noqa: E501

    pr = asyncio.run(_forge(handler).fetch_pull_request(_pr()))
    assert pr.diff_refs == {"base_sha": "b", "head_sha": "h", "start_sha": "s"}
    assert pr.pr_number == 42  # 其余字段不变


def test_fetch_pull_request_returns_pr_when_no_diff_refs():
    # 响应里没有 diff_refs（或非 dict）→ 原样返回 pr，不构造
    handler = lambda r: httpx.Response(200, json={"title": "无 refs"})  # noqa: E731
    pr = asyncio.run(_forge(handler).fetch_pull_request(_pr()))
    assert pr.diff_refs is None


# ── 主动补拉 list_pulls ─────────────────────────────────────────────


def test_list_open_pulls_maps_items():
    def handler(r: httpx.Request) -> httpx.Response:
        assert "state=opened" in str(r.url)
        return httpx.Response(200, json=[
            {
                "iid": 101,
                "title": "feat: polling",
                "sha": "h1",
                "source_branch": "feat-polling",
                "target_branch": "main",
                "web_url": "https://gitlab.example.com/acme/widgets/-/merge_requests/101",
                "diff_refs": {"base_sha": "b0", "head_sha": "h1", "start_sha": "s0"},
                "author": {"username": "bob"},
            }
        ])

    prs = asyncio.run(_forge(handler).list_pulls("7"))
    assert len(prs) == 1
    pr = prs[0]
    assert pr.provider == GITLAB
    assert pr.repo_id == "7"
    assert pr.pr_number == 101
    assert pr.source_branch == "feat-polling"
    assert pr.target_branch == "main"
    assert pr.head_sha == "h1"
    assert pr.author == "bob"
    assert pr.diff_refs == {"base_sha": "b0", "head_sha": "h1", "start_sha": "s0"}


def test_list_pulls_empty_body():
    assert asyncio.run(_forge(lambda r: httpx.Response(200, json=[])).list_pulls("7")) == []


def test_list_pulls_empty_repo_id_returns_empty():
    # repo_id 为空 → 不发请求直接空
    f = _forge(lambda r: httpx.Response(500, json={}))
    assert asyncio.run(f.list_pulls("")) == []


def test_list_pulls_include_closed_uses_state_all():
    def handler(r: httpx.Request) -> httpx.Response:
        assert "state=all" in str(r.url)
        return httpx.Response(200, json=[{
            "iid": 101,
            "title": "feat: closed",
            "sha": "h1",
            "source_branch": "feat-polling",
            "target_branch": "main",
            "web_url": "https://gitlab.example.com/acme/widgets/-/merge_requests/101",
            "diff_refs": {"base_sha": "b0", "head_sha": "h1", "start_sha": "s0"},
            "author": {"username": "bob"},
        }])

    prs = asyncio.run(_forge(handler).list_pulls("7", include_closed=True))
    assert len(prs) == 1
    assert prs[0].pr_number == 101


# ── 评论回写 ────────────────────────────────────────────────────────────


def test_post_inline_skips_without_diff_refs():
    seen: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append({"method": r.method, "url": str(r.url)})
        return httpx.Response(200, json={})

    f = _forge(handler)
    asyncio.run(f.post_inline(_pr(), [{"side": "RIGHT", "line": 5, "body": "x", "path": "a.py"}]))
    assert seen == []  # 无 diff_refs 直接跳过，不发请求


def test_post_inline_builds_position():
    urls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        urls.append(str(r.url))
        return httpx.Response(200, json={})

    f = _forge(handler)
    pr = PullRequest(
        provider="gitlab", repo_id="7", repo_full_name="acme/widgets", web_url="",
        pr_number=42, title="t", source_branch="s", target_branch="t", head_sha="h", base_sha="",
        diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"},
    )
    asyncio.run(f.post_inline(pr, [{"side": "RIGHT", "line": 5, "body": "注释", "path": "src/a.py"}]))  # noqa: E501
    assert len(urls) == 1
    assert "/discussions" in urls[0]  # 命中 GitLab discussions 端点


def test_post_inline_skips_no_line_and_left_uses_old_line():
    urls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        urls.append(str(r.url))
        return httpx.Response(200, json={})

    f = _forge(handler)
    pr = PullRequest(
        provider="gitlab", repo_id="7", repo_full_name="acme/widgets", web_url="",
        pr_number=42, title="t", source_branch="s", target_branch="t", head_sha="h", base_sha="",
        diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"},
    )
    # 第一条无行号 → continue 跳过；第二条 LEFT 走 old_line 分支
    asyncio.run(f.post_inline(pr, [
        {"side": "RIGHT", "line": None, "body": "无行号", "path": "a.py"},
        {"side": "LEFT", "old_line": 3, "body": "删行", "path": "b.py"},
    ]))
    assert len(urls) == 1
    assert '"new_line"' not in urls[0]  # LEFT 不用 new_line


def test_post_summary_uses_notes_endpoint():
    urls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        urls.append(str(r.url))
        assert r.method == "POST"
        return httpx.Response(200, json={})

    f = _forge(handler)
    asyncio.run(f.post_summary(_pr(), "总体还行"))
    assert len(urls) == 1
    assert "/notes" in urls[0]


# ── F3.7：commit status ─────────────────────────────────────────────────


def test_post_commit_status_success():
    url = ""

    def handler(r: httpx.Request) -> httpx.Response:
        nonlocal url
        url = str(r.url)
        assert r.method == "POST"
        assert r.read()  # 有 body
        import json as _json
        body = _json.loads(r.read())
        assert body == {"state": "success", "name": "codereview-ai", "description": ""}
        return httpx.Response(200, json={})

    f = _forge(handler)
    asyncio.run(f.post_commit_status(_pr(), passed=True))
    assert "/api/v4/projects/7/statuses/abc123" in url


def test_post_commit_status_failed():
    states: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        import json as _json
        states.append(_json.loads(r.read())["state"])
        return httpx.Response(200, json={})

    f = _forge(handler)
    asyncio.run(f.post_commit_status(_pr(), passed=False, description="AI 审查 60/100"))
    assert states == ["failed"]


# ── fetch_project / resolve_repo_meta（URL -> 数字项目 ID） ────────────────────


def test_fetch_project_by_namespace_path():
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        assert req.headers["Private-Token"] == TOKEN
        return httpx.Response(200, json={
            "id": 7,
            "path_with_namespace": "acme/widgets",
            "web_url": "https://gitlab.example.com/acme/widgets",
        })

    f = _forge(handler)
    proj = asyncio.run(f.fetch_project("acme/widgets"))
    assert proj["id"] == 7
    assert "/api/v4/projects/acme%2Fwidgets" in captured["url"]  # 路径 URL-encode（/ → %2F）


def test_fetch_project_404_returns_none():
    f = _forge(lambda req: httpx.Response(404, json={}))
    assert asyncio.run(f.fetch_project("missing/proj")) is None


def test_resolve_repo_meta_gitlab_returns_numeric_id():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 7,
            "path_with_namespace": "acme/widgets",
            "web_url": "https://gitlab.example.com/acme/widgets",
        })

    f = _forge(handler)
    meta = asyncio.run(f.resolve_repo_meta(
        "https://gitlab.example.com/acme/widgets/-/merge_requests/42"
    ))
    assert meta == {
        "repo_id": "7",  # GitLab repo_id 是数字项目 ID，非路径
        "repo_full_name": "acme/widgets",
        "web_url": "https://gitlab.example.com/acme/widgets",
    }


def test_resolve_repo_meta_gitlab_404_returns_none():
    f = _forge(lambda req: httpx.Response(404, json={}))
    assert asyncio.run(f.resolve_repo_meta("https://gitlab.example.com/missing/proj")) is None

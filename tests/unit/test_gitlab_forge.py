"""forges/gitlab 测试：payload 解析、changes/fetch 逆转换、空数组重试、评论回写。

用 httpx.MockTransport 注入假 HTTP，全程离线，不碰网络。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from codereview_ai.domain.models import ChangeType, PullRequest
from codereview_ai.forges.gitlab import GitLabForge, _to_file_diff, parse_merge_request_payload
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
    assert d.new_file_content == ""


def test_to_file_diff_deleted_and_renamed():
    deleted = _to_file_diff({"old_path": "a.py", "new_path": "a.py", "deleted_file": True, "diff": "-gone\n"})  # noqa: E501
    assert deleted.change_type == ChangeType.DELETED_FILE
    renamed = _to_file_diff({"old_path": "old.py", "new_path": "new.py", "renamed_file": True, "diff": ""})  # noqa: E501
    assert renamed.change_type == ChangeType.RENAMED_FILE


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


# ── fetch_pull_request 补 diff_refs ─────────────────────────────────────


def test_fetch_pull_request_populates_diff_refs():
    def handler(r: httpx.Request) -> httpx.Response:
        assert API_BASE in str(r.url)
        return httpx.Response(200, json={"diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}})  # noqa: E501

    pr = asyncio.run(_forge(handler).fetch_pull_request(_pr()))
    assert pr.diff_refs == {"base_sha": "b", "head_sha": "h", "start_sha": "s"}
    assert pr.pr_number == 42  # 其余字段不变


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

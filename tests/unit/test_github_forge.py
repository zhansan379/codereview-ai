"""forges/github 测试：payload 解析、files/status 转换、空数组重试、批量 review 回写。

用 httpx.MockTransport 注入假 HTTP，全程离线，不碰网络。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx

from codereview_ai.domain.models import ChangeType, PullRequest
from codereview_ai.forges.github import (
    GitHubForge,
    _new_file_content_from_patch,
    _to_file_diff,
    parse_pull_request_payload,
)
from codereview_ai.forges.signatures import GITHUB

API_BASE = "https://api.github.com"
TOKEN = "ghp-test"


def _sample_pr_payload() -> dict:
    return {
        "action": "opened",
        "sender": {"login": "alice"},
        "repository": {"full_name": "acme/widgets", "html_url": "https://github.com/acme/widgets"},
        "pull_request": {
            "number": 99,
            "title": "feat: add paging",
            "html_url": "https://github.com/acme/widgets/pull/99",
            "head": {"ref": "feat-paging", "sha": "abc123"},
            "base": {"ref": "main", "sha": "9000"},
            "draft": False,
            "head_sha": "abc123",
        },
    }


def _pr() -> PullRequest:
    return parse_pull_request_payload(_sample_pr_payload())  # type: ignore[return-value]


def _forge(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubForge:
    transport = httpx.MockTransport(handler)
    return GitHubForge(API_BASE, TOKEN, httpx.AsyncClient(transport=transport, timeout=5))


# ── payload 解析 ────────────────────────────────────────────────────────


def test_parse_pull_request_maps_fields():
    pr = _pr()
    assert pr.provider == GITHUB
    assert pr.repo_id == "acme/widgets"
    assert pr.pr_number == 99
    assert pr.title == "feat: add paging"
    assert pr.source_branch == "feat-paging"
    assert pr.target_branch == "main"
    assert pr.head_sha == "abc123"
    assert pr.base_sha == "9000"
    assert pr.is_draft is False
    assert pr.author == "alice"


def test_parse_non_pull_request_is_none():
    assert parse_pull_request_payload({"action": "created"}) is None


def test_parse_missing_number_is_none():
    payload = _sample_pr_payload()
    payload["pull_request"].pop("number")
    assert parse_pull_request_payload(payload) is None


def test_should_review_gates_actions():
    f = _forge(lambda r: httpx.Response(500))
    assert f.should_review("opened")
    assert f.should_review("synchronize")
    assert not f.should_review("closed")
    assert not f.should_review("merged")


# ── files → FileDiff / status 转换 ──────────────────────────────────────


def test_to_file_diff_added_derives_content():
    item = {
        "filename": "foo.py",
        "status": "added",
        "additions": 2,
        "deletions": 0,
        "patch": "--- /dev/null\n+++ b/foo.py\n@@ -0,0 +1,2 @@\n+line1\n+line2\n",
    }
    d = _to_file_diff(item)
    assert d.change_type == ChangeType.NEW_FILE
    assert d.new_path == "foo.py"
    assert d.new_file_content == "line1\nline2"


def test_to_file_diff_removed_has_empty_new_path():
    d = _to_file_diff({"filename": "old.py", "status": "removed", "deletions": 3})
    assert d.change_type == ChangeType.DELETED_FILE
    assert d.new_path == ""
    assert d.old_path == "old.py"


def test_to_file_diff_renamed_uses_previous_filename():
    d = _to_file_diff({"filename": "new.py", "previous_filename": "old.py", "status": "renamed", "patch": ""})  # noqa: E501
    assert d.change_type == ChangeType.RENAMED_FILE
    assert d.old_path == "old.py"
    assert d.new_path == "new.py"


def test_to_file_diff_modified_keeps_summary_counts():
    d = _to_file_diff({"filename": "a.py", "status": "modified", "additions": 5, "deletions": 2, "patch": ""})  # noqa: E501
    assert d.change_type == ChangeType.MODIFIED
    assert d.additions == 5
    assert d.deletions == 2


def test_new_file_content_ignores_hunk_header():
    assert _new_file_content_from_patch("@@ -0,0 +1,1 @@\n+x\n", ChangeType.MODIFIED) == ""


# ── fetch_files 拉取 + 空数组重试 ──────────────────────────────────────


def test_fetch_files_converts_items():
    handler = lambda r: httpx.Response(200, json=[  # noqa: E731
        {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0,
         "patch": "--- a.py\n+++ b.py\n@@ -1 +1,2 @@\n ctx\n+added\n"}
    ])
    diffs = asyncio.run(_forge(handler).fetch_files(_pr()))
    assert len(diffs) == 1
    assert diffs[0].new_path == "a.py"


def test_fetch_files_retries_on_empty_then_succeeds():
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:  # 前两次空，第三次有数据
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"filename": "x", "status": "modified", "patch": "+y\n"}])

    diffs = asyncio.run(_forge(handler).fetch_files(_pr()))
    assert len(calls) == 3
    assert len(diffs) == 1


# ── fetch_pull_request 补权威 head/base sha ─────────────────────────────


def test_fetch_pull_request_populates_authoritative_shas():
    def handler(r: httpx.Request) -> httpx.Response:
        assert "/pulls/99" in str(r.url)
        return httpx.Response(200, json={"head": {"sha": "HEAD1"}, "base": {"sha": "BASE1"}, "title": "t2"})  # noqa: E501

    pr = asyncio.run(_forge(handler).fetch_pull_request(_pr()))
    assert pr.head_sha == "HEAD1"
    assert pr.base_sha == "BASE1"
    assert pr.title == "t2"
    assert pr.pr_number == 99  # 其余字段不变


# ── 回写评论 ────────────────────────────────────────────────────────────


def test_post_inline_skips_without_head_sha():
    seen: list[dict] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, json={})

    f = _forge(handler)
    pr = PullRequest(provider="github", repo_id="acme/widgets", repo_full_name="", web_url="",
                     pr_number=99, title="", source_branch="", target_branch="", head_sha="", base_sha="")  # noqa: E501
    asyncio.run(f.post_inline(pr, [{"side": "RIGHT", "line": 5, "body": "x", "path": "a.py"}]))
    assert seen == []  # 无 head_sha 直接跳过


def test_post_inline_builds_batch_review():
    sent: list[bytes] = []

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append(r.content)
        return httpx.Response(200, json={})

    f = _forge(handler)
    pr = PullRequest(provider="github", repo_id="acme/widgets", repo_full_name="", web_url="",
                     pr_number=99, title="", source_branch="", target_branch="", head_sha="h", base_sha="b")  # noqa: E501
    asyncio.run(f.post_inline(pr, [
        {"side": "RIGHT", "line": 5, "body": "新增行", "path": "src/a.py"},
        {"side": "LEFT", "old_line": 3, "body": "删行", "path": "src/a.py"},
    ]))
    payload = json.loads(sent[-1])
    assert payload["commit_id"] == "h"
    assert payload["event"] == "COMMENT"
    # RIGHT 用 line、LEFT 用 old_line，side 保留
    assert payload["comments"][0] == {"path": "src/a.py", "line": 5, "side": "RIGHT", "body": "新增行"}  # noqa: E501
    assert payload["comments"][1] == {"path": "src/a.py", "line": 3, "side": "LEFT", "body": "删行"}  # noqa: E501


def test_post_summary_uses_issues_comments():
    urls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        urls.append(str(r.url))
        assert r.method == "POST"
        return httpx.Response(200, json={})

    f = _forge(handler)
    asyncio.run(f.post_summary(_pr(), "总体还行"))
    assert len(urls) == 1
    assert "/issues/" in urls[0]

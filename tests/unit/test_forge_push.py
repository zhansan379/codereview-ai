"""forges push 轨（§7.7）测试：事件解析、差量三分支、commit 总结回写方言差异。

离线：httpx.MockTransport 断言 URL/方法/字段；三分支（删除/新分支/compare）、
GitLab `note` vs GitHub `body` 方言差异、删分支返回空。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx

from codereview_ai.domain.models import ChangeType, PushEvent
from codereview_ai.forges.github import (
    GitHubForge,
)
from codereview_ai.forges.github import (
    parse_push_event_payload as parse_gh_push,
)
from codereview_ai.forges.gitlab import (
    GitLabForge,
)
from codereview_ai.forges.gitlab import (
    parse_push_event_payload as parse_gl_push,
)
from codereview_ai.forges.signatures import GITHUB, GITLAB

GL_BASE = "https://gitlab.example.com"
GH_BASE = "https://api.github.com"
TOKEN = "tok"

ALL_ZERO = "0000000000000000000000000000000000000000"


def _gl_push(**over) -> dict:
    payload = {
        "object_kind": "push",
        "user_username": "alice",
        "project": {"id": 7, "path_with_namespace": "acme/widgets"},
        "ref": "refs/heads/main",
        "before": "aaaa",
        "after": "bbbb",
        "commits": [{"id": "bbbb", "message": "fix: x", "author": {"name": "Bob"}}],
    }
    payload.update(over)
    return payload


def _gh_push(**over) -> dict:
    payload = {
        "ref": "refs/heads/main",
        "before": "aaaa",
        "after": "bbbb",
        "repository": {"full_name": "acme/widgets"},
        "commits": [{"id": "bbbb", "message": "fix: x", "author": {"name": "Bob"}}],
        "pusher": {"name": "alice"},
    }
    payload.update(over)
    return payload


def _forge(handler: Callable[[httpx.Request], httpx.Response], provider: str):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5)
    if provider == GITLAB:
        return GitLabForge(GL_BASE, TOKEN, client)
    return GitHubForge(GH_BASE, TOKEN, client)


# ── 事件解析 ────────────────────────────────────────────────────────────


def test_gitlab_parse_push():
    ev = parse_gl_push(_gl_push())
    assert ev.provider == GITLAB
    assert ev.repo_id == "7"
    assert ev.branch == "main"
    assert ev.before == "aaaa" and ev.after == "bbbb"
    assert ev.commits[0].sha == "bbbb" and ev.pusher == "alice"


def test_gitlab_parse_non_push_is_none():
    assert parse_gl_push({"object_kind": "merge_request"}) is None


def test_github_parse_push():
    ev = parse_gh_push(_gh_push())
    assert ev.provider == GITHUB
    assert ev.repo_id == "acme/widgets"
    assert ev.branch == "main"
    assert ev.pusher == "alice"


def test_github_ref_no_heads_prefix_clears_branch():
    ev = parse_gh_push(_gh_push(ref="refs/tags/v1"))
    assert ev.branch == ""


# ── 差量三分支 ──────────────────────────────────────────────────────────


def test_push_changes_returns_empty_when_after_all_zero():
    async def run():
        f = _forge(lambda r: httpx.Response(999), GITLAB)
        return await f.get_push_changes(PushEvent(
            provider=GITLAB, repo_id="7", repo_full_name="a/b", branch="main",
            before="aaaa", after=ALL_ZERO,
        ))

    assert asyncio.run(run()) == []


def test_push_event_is_dataclass():
    from dataclasses import is_dataclass

    assert is_dataclass(PushEvent)
    assert PushEvent(provider="gitlab", repo_id="7", repo_full_name="a/b",
                     branch="main", before="a", after="b").pusher == ""


def test_gitlab_compare_uses_diffs_key_and_note_comment():
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.path.endswith("/compare"):
            return httpx.Response(200, json={
                "diffs": [{
                    "old_path": "a.py", "new_path": "a.py", "new_file": True,
                    "deleted_file": False, "renamed_file": False,
                    "diff": "--- a.py\n+++ b.py\n@@ -0,0 +1 @@\n+x\n",
                }]
            })
        return httpx.Response(200, json={})

    async def run():
        f = _forge(handler, GITLAB)
        diffs = await f.get_push_changes(PushEvent(
            provider=GITLAB, repo_id="7", repo_full_name="a/b", branch="main",
            before="aaaa", after="bbbb",
        ))
        assert len(diffs) == 1 and diffs[0].change_type == ChangeType.NEW_FILE
        assert str(seen[0].url).count("from=aaaa&to=bbbb") == 1
        await f.post_commit_summary(
            PushEvent(provider=GITLAB, repo_id="7", repo_full_name="a/b",
                      branch="main", before="aaaa", after="bbbb"),
            "总体还行",
        )
        assert "repository/commits/bbbb/comments" in str(seen[1].url)
        assert seen[1].read().decode("utf-8").count("note") == 1

    asyncio.run(run())


def test_gitlab_new_branch_hits_single_commit_diff():
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, json=[
            {"old_path": "x.py", "new_path": "x.py", "diff": "+y\n"}
        ])

    async def run():
        f = _forge(handler, GITLAB)
        ev = parse_gl_push(_gl_push(before=ALL_ZERO))
        diffs = await f.get_first_commit_changes(ev)
        assert len(diffs) == 1
        assert "/repository/commits/bbbb/diff" in seen[0]

    asyncio.run(run())


def test_github_compare_uses_files_key_and_body_comment():
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if "compare/" in r.url.path:
            return httpx.Response(200, json={
                "files": [{"filename": "a.py", "status": "modified",
                           "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n x\n+y\n"}]
            })
        return httpx.Response(200, json={})

    async def run():
        f = _forge(handler, GITHUB)
        ev = parse_gh_push(_gh_push())
        diffs = await f.get_push_changes(ev)
        assert len(diffs) == 1 and diffs[0].new_path == "a.py"
        assert "compare/aaaa...bbbb" in str(seen[0].url)
        await f.post_commit_summary(ev, "总结")
        assert "commits/bbbb/comments" in str(seen[1].url)
        assert seen[1].read().decode("utf-8").count("body") == 1

    asyncio.run(run())


def test_github_new_branch_single_commit():
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(str(r.url))
        return httpx.Response(200, json={
            "sha": "bbbb",
            "files": [{"filename": "n.py", "status": "added", "patch": "+new\n"}],
        })

    async def run():
        f = _forge(handler, GITHUB)
        ev = parse_gh_push(_gh_push(before=ALL_ZERO))
        diffs = await f.get_first_commit_changes(ev)
        assert len(diffs) == 1 and diffs[0].change_type == ChangeType.NEW_FILE
        assert "commits/bbbb" in seen[0]

    asyncio.run(run())

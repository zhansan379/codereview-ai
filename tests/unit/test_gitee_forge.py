"""forges/gitee 测试：payload 解析、files 转换、补拉、position 换算、评论回写。

用 httpx.MockTransport 注入假 HTTP，全程离线，不碰网络。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx

from codereview_ai.domain.models import ChangeType
from codereview_ai.forges.gitee import (
    GiteeForge,
    _position_for,
    _to_file_diff,
    parse_pull_request_payload,
    pull_request_from_item,
)
from codereview_ai.forges.signatures import GITEE

API_BASE = "https://gitee.com/api/v5"
TOKEN = "gitee-token"


def _sample_pr_payload() -> dict:
    return {
        "action": "open",
        "sender": {"login": "alice"},
        "repository": {"full_name": "acme/widgets", "html_url": "https://gitee.com/acme/widgets"},
        "pull_request": {
            "number": 7,
            "title": "feat: add paging",
            "html_url": "https://gitee.com/acme/widgets/pull/7",
            "head": {"ref": "feat-paging", "sha": "abc123"},
            "base": {"ref": "master", "sha": "9000"},
            "user": {"login": "bob"},
            "draft": False,
        },
    }


def _pr():
    return parse_pull_request_payload(_sample_pr_payload())  # type: ignore[return-value]


def _forge(handler: Callable[[httpx.Request], httpx.Response]) -> GiteeForge:
    transport = httpx.MockTransport(handler)
    return GiteeForge(API_BASE, TOKEN, httpx.AsyncClient(transport=transport, timeout=5))


def _files_payload() -> list[dict]:
    return [{
        "filename": "src/app.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
        "patch": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n import os\n-def foo():\n+def foo():\n+    return 1\n def bar():",
    }]


# ── payload 解析 ────────────────────────────────────────────────────────


def test_parse_pull_request_maps_fields():
    pr = _pr()
    assert pr.provider == GITEE
    assert pr.repo_id == "acme/widgets"
    assert pr.pr_number == 7
    assert pr.title == "feat: add paging"
    assert pr.source_branch == "feat-paging"
    assert pr.target_branch == "master"
    assert pr.head_sha == "abc123"
    assert pr.base_sha == "9000"
    assert pr.is_draft is False
    assert pr.author == "bob"  # pull_request.user.login 优先于 sender


def test_parse_non_pull_request_is_none():
    assert parse_pull_request_payload({"action": "comment"}) is None


def test_pull_request_from_item_maps_fields():
    item = _sample_pr_payload()["pull_request"]
    item["base"]["repo"] = {"full_name": "acme/widgets"}
    pr = pull_request_from_item(item)
    assert pr is not None
    assert pr.repo_id == "acme/widgets"
    assert pr.head_sha == "abc123"
    assert pr.author == "bob"


# ── files 转换 ──────────────────────────────────────────────────────────


def test_to_file_diff_restores_new_content():
    fd = _to_file_diff(_files_payload()[0])
    assert fd.change_type is ChangeType.MODIFIED
    assert fd.new_path == "src/app.py"
    assert fd.additions == 2 and fd.deletions == 1
    assert fd.new_file_content.splitlines() == [
        "import os", "def foo():", "    return 1", "def bar():",
    ]


def test_to_file_diff_deleted_file():
    fd = _to_file_diff({"filename": "gone.py", "status": "removed", "patch": ""})
    assert fd.change_type is ChangeType.DELETED_FILE
    assert fd.new_path == "" and fd.old_path == "gone.py"


# ── position 换算（Gitee 行级评论的 patch 内行号）────────────────────────


_PATCH = "--- a/f\n+++ b/f\n@@ -1,3 +1,4 @@\n import os\n-def foo():\n+def foo():\n+    return 1\n def bar():"


def test_position_counts_metadata_lines():
    # patch 逐行（1 起）：1 `---`、2 `+++`、3 `@@`、4 上下文、5 `-`、6 `+`、7 `+`、8 上下文
    # 新侧行号：import os=1、def foo()=2、return 1=3、def bar()=4；旧侧：foo()=2
    assert _position_for(_PATCH, side="RIGHT", line=3) == 7   # 新侧第 3 行 = "+    return 1"
    assert _position_for(_PATCH, side="RIGHT", line=1) == 4   # 上下文行（新侧第 1 行）
    assert _position_for(_PATCH, side="LEFT", line=2) == 5    # 旧侧第 2 行 = "-def foo():"


def test_position_not_in_diff_is_none():
    assert _position_for(_PATCH, side="RIGHT", line=99) is None
    assert _position_for(_PATCH, side="RIGHT", line=None) is None


# ── REST 拉取 / 回写 ────────────────────────────────────────────────────


def test_list_pulls_passes_state_and_token():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{
            "number": 7, "title": "t", "html_url": "u",
            "head": {"ref": "b", "sha": "abc123"},
            "base": {"ref": "m", "sha": "9000", "repo": {"full_name": "acme/widgets"}},
            "user": {"login": "bob"},
        }])

    prs = asyncio.run(_forge(handler).list_pulls("acme/widgets"))
    assert seen["url"].startswith(f"{API_BASE}/repos/acme/widgets/pulls?")
    assert "state=open" in seen["url"] and f"access_token={TOKEN}" in seen["url"]
    assert len(prs) == 1 and prs[0].head_sha == "abc123"


def test_fetch_files_retries_on_empty():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=(_files_payload() if calls["n"] >= 2 else []))

    files = asyncio.run(_forge(handler).fetch_files(_pr()))
    assert calls["n"] == 2
    assert len(files) == 1 and files[0].new_path == "src/app.py"


def test_post_inline_converts_anchor_to_position():
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=_files_payload())
        assert request.method == "POST"
        posted.append(json.loads(request.content.decode()))
        return httpx.Response(201, json={})

    pr = _pr()
    asyncio.run(_forge(handler).post_inline(pr, [{
        "path": "src/app.py", "line": 3, "side": "RIGHT", "body": "nits",
    }]))
    assert len(posted) == 1
    assert posted[0]["path"] == "src/app.py"
    assert posted[0]["position"] == 7  # 与 test_position_counts_metadata_lines 同口径
    assert posted[0]["commit_id"] == "abc123"
    assert posted[0]["access_token"] == TOKEN


def test_list_comments_collects_bodies():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "access_token=" in str(request.url)
        return httpx.Response(200, json=[{"body": "already"}, {"body": ""}])

    assert asyncio.run(_forge(handler).list_comments(_pr())) == ["already"]


def test_resolve_repo_meta_from_url():
    forge = _forge(lambda request: httpx.Response(500))
    meta = asyncio.run(forge.resolve_repo_meta("https://gitee.com/acme/widgets/pulls/7"))
    assert meta == {
        "repo_id": "acme/widgets",
        "repo_full_name": "acme/widgets",
        "web_url": "https://gitee.com/acme/widgets/pulls/7",
    }
    assert asyncio.run(forge.resolve_repo_meta("https://gitee.com/only")) is None

"""真文富化（forges/base.enrich_new_file_contents）+ 三家适配器 fetch_file_content 单测。

背景：patch 重建的 new_file_content 对未被 hunk 覆盖的行填空行，直接跑 ruff 会雪崩
误报（报告 #62 一次性 80+ 条假 finding）。worker 静态分析前按文件并发拉平台真实全文
覆盖；本文件离线验证富化语义与三家适配器的单文件拉取原语。

全程离线：适配器用 httpx.MockTransport 注入假 HTTP；富化助手用 stub Forge。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import httpx

from codereview_ai.domain.models import ChangeType, FileDiff, PullRequest
from codereview_ai.forges.base import ForgeAdapter, enrich_new_file_contents
from codereview_ai.forges.gitee import GiteeForge
from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.gitlab import GitLabForge


def _fd(path: str, content: str = "patch-reconstructed\n") -> FileDiff:
    return FileDiff(
        old_path=path, new_path=path,
        diff="---\n+++\n@@ +1 +1 @@\n+x",
        additions=1, deletions=0, change_type=ChangeType.MODIFIED,
        new_file_content=content,
    )


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo_id="o/r", repo_full_name="o/r", web_url="u",
        pr_number=1, title="t", source_branch="s", target_branch="m",
        head_sha="a", base_sha="b",
    )


class _StubForge(ForgeAdapter):
    """fetch_file_content 可编程的 stub：contents 命中返回真文、fail_paths 抛错、其余空。"""

    name = "github"

    def __init__(
        self, contents: dict[str, str] | None = None, fail_paths: tuple[str, ...] = ()
    ) -> None:
        self.contents = contents or {}
        self.fail_paths = set(fail_paths)
        self.calls: list[tuple[str, str, str]] = []

    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        return None

    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        return []

    async def post_summary(self, pr: PullRequest, body: str) -> None:
        return None

    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        return None

    async def fetch_file_content(self, repo_id: str, path: str, ref: str) -> str:
        self.calls.append((repo_id, path, ref))
        if path in self.fail_paths:
            raise RuntimeError("boom")
        return self.contents.get(path, "")


# ── enrich_new_file_contents：替换/保留/跳过语义 ─────────────────────────


async def test_enrich_replaces_hit_and_keeps_miss():
    """拉到真文的文件 replace 重建内容；拉空/失败的原样保留（对象都不换）。"""
    forge = _StubForge({"a.py": "REAL A\n"})
    diffs = [_fd("a.py"), _fd("b.py")]
    out = await enrich_new_file_contents(forge, diffs, "o/r", "sha1")
    assert out[0].new_file_content == "REAL A\n" and out[0] is not diffs[0]
    assert out[1].new_file_content == "patch-reconstructed\n" and out[1] is diffs[1]
    # 每个目标路径恰好拉一次
    assert forge.calls == [("o/r", "a.py", "sha1"), ("o/r", "b.py", "sha1")]


async def test_enrich_failure_keeps_patch_content_and_never_raises():
    """单文件抛错 → 该文件留用 patch 重建，整体不抛。"""
    forge = _StubForge({}, fail_paths=("x.py",))
    diffs = [_fd("x.py")]
    out = await enrich_new_file_contents(forge, diffs, "o/r", "sha1")
    assert out == diffs


async def test_enrich_skips_deleted_files_and_empty_ref():
    """删除文件无新侧不拉；ref 缺失直接原样返回（零请求）。"""
    forge = _StubForge({"a.py": "REAL\n"})
    deleted = FileDiff(
        old_path="a.py", new_path="", diff="---", additions=0, deletions=3,
        change_type=ChangeType.DELETED_FILE, new_file_content="",
    )
    diffs = [deleted]
    assert await enrich_new_file_contents(forge, diffs, "o/r", "sha1") == diffs
    assert forge.calls == []
    assert await enrich_new_file_contents(forge, diffs, "o/r", "") == diffs
    assert forge.calls == []


async def test_enrich_dedupes_paths():
    """同路径多个 diff 只拉一次，全部替换。"""
    forge = _StubForge({"a.py": "REAL\n"})
    diffs = [_fd("a.py"), _fd("a.py")]
    out = await enrich_new_file_contents(forge, diffs, "o/r", "sha1")
    assert len(forge.calls) == 1
    assert all(d.new_file_content == "REAL\n" for d in out)


async def test_enrich_duck_typed_forge_without_method_never_raises():
    """没实现 fetch_file_content 的鸭子型 forge（旧测试桩）→ 按失败降级，不炸。"""
    class _Legacy:
        pass

    diffs = [_fd("a.py")]
    out = await enrich_new_file_contents(_Legacy(), diffs, "o/r", "sha1")  # type: ignore[arg-type]
    assert out == diffs


# ── 适配器 fetch_file_content（MockTransport 离线）─────────────────────────


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_github_fetch_file_content_raw_text():
    """GitHub：contents API + Accept raw 直出文件字节，带 ref 参数与 Bearer 头。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/o/r/contents/src/a.py"
        assert request.url.params["ref"] == "sha9"
        assert request.headers["Accept"] == "application/vnd.github.raw"
        assert request.headers["Authorization"] == "Bearer gh-token"
        return httpx.Response(200, text="REAL github\n")

    forge = GitHubForge("https://api.github.com", "gh-token", _http(handler))
    assert await forge.fetch_file_content("o/r", "src/a.py", "sha9") == "REAL github\n"


async def test_gitlab_fetch_file_content_decodes_base64():
    """GitLab：repository files API（路径整体编码），JSON base64 解码出全文。"""
    payload = base64.b64encode(b"REAL gitlab\n").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitlab.example.com"
        assert "src%2Fa.py" in str(request.url)  # / 已转义
        assert request.url.params["ref"] == "sha9"
        return httpx.Response(200, json={"content": payload})

    forge = GitLabForge("https://gitlab.example.com", "glpat-x", _http(handler))
    assert await forge.fetch_file_content("1", "src/a.py", "sha9") == "REAL gitlab\n"


async def test_gitee_fetch_file_content_decodes_base64():
    """Gitee：contents API（access_token 走 query），JSON base64 解码出全文。"""
    payload = base64.b64encode(b"REAL gitee\n").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitee.com"
        assert request.url.path == "/api/v5/repos/o/r/contents/src/a.py"
        assert request.url.params["ref"] == "sha9"
        assert request.url.params["access_token"] == "gt-token"
        return httpx.Response(200, json={"content": payload})

    forge = GiteeForge("https://gitee.com/api/v5", "gt-token", _http(handler))
    assert await forge.fetch_file_content("o/r", "src/a.py", "sha9") == "REAL gitee\n"


async def test_adapter_fetch_file_content_guards_blank_inputs():
    """缺 repo_id/path/ref 直接空串（不发请求），上层留用 patch 重建。"""

    class _NoopTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("不应发请求")

    gh = GitHubForge("https://api.github.com", "t",
                     httpx.AsyncClient(transport=_NoopTransport()))
    assert await gh.fetch_file_content("", "a.py", "sha") == ""
    assert await gh.fetch_file_content("o/r", "", "sha") == ""
    assert await gh.fetch_file_content("o/r", "a.py", "") == ""

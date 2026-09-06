"""M6 验收套件 · 审查管线侧（PRD 标准 1 / 4 / 5）。

- 标准 1：diff → 至少 1 条落在正确行号上的行级 inline + 1 条总结评论
  （"90 秒内"为真实 GitLab 实测项，离线只证这条链成立）。
- 标准 4：追加 commit → 第二次审查只审增量 diff，且不重复上次报过的问题。
- 标准 5：GitHub 全部 inline 评论通过**单次** review API 提交（真实适配器 + MockTransport）。

全程离线：临时 SQLite / httpx.MockTransport / fake LLM。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine as _AE

from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.forges.github import GitHubForge
from codereview_ai.review.increments import finding_fingerprint
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import process_raw_event

# ── 标准 1：diff → 行级 inline + 总结（含正确行号）────────────────────────


class _InlineForge(ForgeAdapter):
    name = "gitlab"

    def __init__(self) -> None:
        self.posted_inline: list[list[dict]] = []
        self.posted_summary: list[str] = []

    @staticmethod
    def _pr():
        from codereview_ai.domain.models import PullRequest

        return PullRequest(
            provider="gitlab", repo_id="3", repo_full_name="acme/x",
            web_url="", pr_number=3, title="t", source_branch="s", target_branch="t",
            head_sha="h", base_sha="",
            diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"},
        )

    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
        return self._pr()

    @staticmethod
    def should_review(action: str) -> bool:
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        return pr

    async def fetch_files(self, pr):
        from codereview_ai.domain.models import ChangeType, FileDiff

        return [FileDiff(old_path="a.py", new_path="a.py",
                         diff="---\n+++\n@@ -0,0 +1,3 @@\n+ctx\n+bug line\n",
                         additions=3, deletions=0, change_type=ChangeType.NEW_FILE)]

    async def post_inline(self, pr, comments: list[dict]) -> None:
        self.posted_inline.append(comments)

    async def post_summary(self, pr, body: str) -> None:
        self.posted_summary.append(body)


class _BugReviewer:
    """在 new_file_content 的第 2 行（已新增行）锚定一条 high 问题。"""

    async def review(self, *, pr, commits_text, diffs):
        from codereview_ai.domain.models import (
            Category,
            Finding,
            ReviewResult,
            ReviewScores,
            Severity,
        )

        result = ReviewResult(
            summary="发现了问题",
            scores=ReviewScores(correctness=30, security=20, practices=15,
                                performance=4, commit_quality=3),
        )
        result.findings.append(Finding(
            content="潜在空指针", category=Category.BUG, severity=Severity.HIGH,
            existing_code="+bug line\n", file="a.py", line=2,
        ))
        return result


async def test_c1_diff_produces_inline_correct_line_plus_summary():
    forge = _InlineForge()
    reviewer = _BugReviewer()
    payload = json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 3,
                              "last_commit": {"id": "h"}},
    }).encode()
    await process_raw_event(forge, reviewer, payload)  # type: ignore[arg-type]

    # 至少 1 条行级 inline，且落在正确的新增行号上
    assert forge.posted_inline, "应有行级 inline 评论"
    posted = [c for batch in forge.posted_inline for c in batch]
    assert any(c["line"] == 2 for c in posted), "应有落在正确行号 2 的 inline"
    assert len(forge.posted_summary) == 1  # 1 条总结评论


# ── 标准 4：追加 commit → 只审增量 + 不重复上次问题 ───────────────────────


class _IncrementalForge(ForgeAdapter):
    """head=h1 时只返回增量（追加的 b.py），不含首轮的 a.py。"""

    name = "gitlab"

    def __init__(self) -> None:
        self.posted_inline: list[list[dict]] = []
        self.posted_summary: list[str] = []

    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
        from codereview_ai.domain.models import PullRequest

        return PullRequest(
            provider="gitlab", repo_id="9", repo_full_name="acme/y",
            web_url="", pr_number=9, title="t", source_branch="s", target_branch="t",
            head_sha=str((oa.get("last_commit") or {}).get("id") or "h"),
            base_sha="", diff_refs=None,
        )

    @staticmethod
    def should_review(action: str) -> bool:
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        return pr

    async def fetch_files(self, pr):
        from codereview_ai.domain.models import ChangeType, FileDiff

        # 只返回增量：追加的 b.py（首轮的 a.py 不在这次 diff 里）
        return [FileDiff(old_path="b.py", new_path="b.py",
                         diff="---\n+++\n@@ -0,0 +1,2 @@\n+ctx\n+new stuff\n",
                         additions=2, deletions=0, change_type=ChangeType.NEW_FILE)]

    async def post_inline(self, pr, comments: list[dict]) -> None:
        self.posted_inline.append(comments)

    async def post_summary(self, pr, body: str) -> None:
        self.posted_summary.append(body)


class _IncrementalReviewer:
    """记录所见 diff；本轮返回一条新问题 + 一条与上次相同的老问题。"""

    def __init__(self, forge: _IncrementalForge) -> None:
        self.forge = forge
        self.n_diffs = 0
        self.diff_paths: list[str] = []

    async def review(self, *, pr, commits_text, diffs):
        from codereview_ai.domain.models import (
            Category,
            Finding,
            ReviewResult,
            ReviewScores,
            Severity,
        )

        self.n_diffs = len(diffs)
        self.diff_paths = [d.new_path for d in diffs]
        result = ReviewResult(
            summary="评估增量",
            scores=ReviewScores(correctness=30, security=20, practices=15,
                                performance=4, commit_quality=3),
        )
        # 与上次相同内容的老问题：应被指纹去重，不再重复报告
        result.findings.append(Finding(
            content="上次已报的缓存问题", category=Category.BUG, severity=Severity.HIGH,
            existing_code="+old\n", file="a.py", line=1,
        ))
        # 本轮真正的新问题
        result.findings.append(Finding(
            content="新增逻辑缺陷", category=Category.BUG, severity=Severity.HIGH,
            existing_code="+new stuff\n", file="b.py", line=2,
        ))
        return result


async def _seed_prior_completed(engine: _AE) -> None:
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(provider="gitlab", repo_id="9", pr_number=9, event_type="mr",
                       branch="main", head_sha="h0", state="completed", score_total=70)
        s.add(t)
        await s.flush()
        from codereview_ai.domain.models import Category, Finding, Severity

        old = Finding(content="上次已报的缓存问题", category=Category.BUG,
                      severity=Severity.HIGH, existing_code="+old\n", file="a.py", line=1)
        s.add(ReviewFinding(task_id=t.id, fingerprint=finding_fingerprint(old),
                            severity="high", category="bug", file="a.py", title="t"))
        await s.commit()


async def test_c4_append_commit_reviews_only_increment_and_no_repost(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'inc.db'}")
    await init_db(engine)
    try:
        await _seed_prior_completed(engine)
        repo = ReviewRepository(engine)
        forge = _IncrementalForge()
        reviewer = _IncrementalReviewer(forge)

        # 追加 commit：head 由 h0 → h1，链有效 → 增量
        payload = json.dumps({
            "object_kind": "merge_request",
            "object_attributes": {"action": "update", "iid": 9,
                                  "last_commit": {"id": "h1"}},
        }).encode()
        await process_raw_event(  # type: ignore[arg-type]
            forge, reviewer, payload, review_repo=repo, chain_valid=lambda a, b: True,
        )

        # 第二次审查只拿到增量 diff（仅 b.py）
        assert reviewer.n_diffs == 1
        assert reviewer.diff_paths == ["b.py"]

        # 不重复上次问题；只报告本轮新问题
        bodies = [c["body"] for batch in forge.posted_inline for c in batch]
        assert "新增逻辑缺陷" in bodies
        assert "上次已报的缓存问题" not in bodies
    finally:
        await engine.dispose()


# ── 标准 5：GitHub 单次 review API 提交全部 inline ───────────────────────


async def test_c5_github_single_review_api_batch():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": 1})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        forge = GitHubForge("https://api.github.com", "tok", client)
        from codereview_ai.domain.models import PullRequest

        pr = PullRequest(provider="github", repo_id="acme/foo", repo_full_name="acme/foo",
                         web_url="", pr_number=7, title="t", source_branch="s",
                         target_branch="t", head_sha="abcd", base_sha="")
        await forge.post_inline(pr, [
            {"path": "a.py", "line": 10, "side": "RIGHT", "body": "p1"},
            {"path": "a.py", "line": 20, "side": "RIGHT", "body": "p2"},
            {"path": "b.py", "line": 1, "side": "RIGHT", "body": "p3"},
        ])

    # 恰好一次单次 review API，payload 带全部 inline
    assert len(calls) == 1
    assert str(calls[0].url).endswith("/repos/acme/foo/pulls/7/reviews")
    payload = calls[0].read().decode("utf-8")
    body = json.loads(payload)
    assert body["event"] == "COMMENT"
    assert body["commit_id"] == "abcd"
    assert len(body["comments"]) == 3

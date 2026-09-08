"""review/result_writer 测试：findings 分区、评论 dict 转换、总结 Markdown。

全程离线：forge 用内存 fake，只断言调用而非真实网络。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from codereview_ai.domain.models import (
    Category,
    ChangeType,
    FileDiff,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
    Severity,
)
from codereview_ai.review.result_writer import (
    ResultWriter,
    build_summary_markdown,
    finding_to_comment,
    partition_findings,
    redeliver,
    review_fingerprint,
)


def _diff(new_path: str, raw: str) -> FileDiff:
    return FileDiff(old_path=new_path, new_path=new_path, diff=raw, additions=1, deletions=0,
                    change_type=ChangeType.MODIFIED)


def _pr() -> PullRequest:
    return PullRequest(
        provider="gitlab", repo_id="7", repo_full_name="acme/widgets", web_url="",
        pr_number=42, title="t", source_branch="s", target_branch="t",
        head_sha="h", base_sha="b",
        diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"},
    )


DIFF = "--- a.py\n+++ b.py\n@@ -1 +1,2 @@\n ctx\n+added\n"


def _finding(file: str = "a.py", line: int | None = 2, side: str = "RIGHT",
             content: str = "bug") -> Finding:
    return Finding(
        content=content,
        category=Category.BUG,
        severity=Severity.HIGH,
        existing_code="added",
        file=file,
        line=line,
        old_line=None,
        side=side,
    )


# ── partition_findings ──────────────────────────────────────────────────


def test_partition_inline_when_line_commentable():
    inline, textual = partition_findings([_finding(line=2)], [_diff("a.py", DIFF)])
    assert len(inline) == 1  # 新增行 2 在可评论集合内
    assert textual == []


def test_partition_demotes_unresolved_line():
    inline, textual = partition_findings([_finding(line=None)], [_diff("a.py", DIFF)])
    assert inline == []
    assert len(textual) == 1


def test_partition_demotes_out_of_hunk_line():
    inline, textual = partition_findings([_finding(line=99, content="越界")], [_diff("a.py", DIFF)])
    assert inline == []
    assert len(textual) == 1  # 行号不在 hunk 内 → 并入总结


def test_partition_ignores_wrong_file():
    inline, textual = partition_findings([_finding(file="other.py", line=2)], [_diff("a.py", DIFF)])
    assert inline == []
    assert len(textual) == 1


# ── finding_to_comment ──────────────────────────────────────────────────


def test_finding_to_comment_keeps_side_and_path():
    c = finding_to_comment(_finding(line=2))
    assert c["path"] == "a.py"
    assert c["side"] == "RIGHT"
    assert c["line"] == 2


# ── build_summary_markdown ──────────────────────────────────────────────


def test_summary_contains_scores_heading_and_textual():
    result = ReviewResult(summary="整体良好", scores=ReviewScores(correctness=30, security=20, practices=15, performance=4, commit_quality=3))  # noqa: E501
    text = build_summary_markdown(_pr(), [_finding(line=None, content="无法定位的提醒")], result)
    assert "72 / 100" in text  # 30+20+15+4+3
    assert "整体良好" in text
    assert "无法定位的提醒" in text
    assert "acme/widgets#42" in text


# ── ResultWriter 编排 ───────────────────────────────────────────────────


def test_writer_posts_inline_then_summary():
    class FakeForge:
        def __init__(self) -> None:
            self.inline_calls: list[list[dict]] = []
            self.summary_calls: list[str] = []

        async def post_inline(self, pr, comments: list[dict]) -> None:
            self.inline_calls.append(comments)

        async def post_summary(self, pr, body: str) -> None:
            self.summary_calls.append(body)

    result = ReviewResult(summary="OK", scores=ReviewScores(correctness=30, security=20, practices=15, performance=4, commit_quality=3))  # noqa: E501
    result.findings.append(_finding(line=2))  # 可锚定 → inline
    result.findings.append(_finding(line=None, content="总结项"))  # 无法锚定 → summary

    forge = FakeForge()
    writer = ResultWriter(forge)  # type: ignore[arg-type]
    asyncio.run(writer.write(_pr(), [_diff("a.py", DIFF)], result))

    assert len(forge.inline_calls) == 1
    assert forge.inline_calls[0][0]["line"] == 2
    assert "总结项" in forge.summary_calls[0]


# ── 回写网络重试兜底（ConnectError 等瞬时故障）─────────────────────────


def test_writer_retries_transient_connecterror_then_succeeds():
    """总结评论 ConnectError 两次后成功 → 重试兜住，不丢已算好的审查。"""
    class FakeForge:
        def __init__(self) -> None:
            self.summary_attempts = 0

        async def post_inline(self, pr, comments: list[dict]) -> None:
            pass

        async def post_summary(self, pr, body: str) -> None:
            self.summary_attempts += 1
            if self.summary_attempts <= 2:
                raise httpx.ConnectError("")  # 模拟本次空消息的瞬时连接故障

    result = ReviewResult(summary="OK", scores=ReviewScores(correctness=30, security=20, practices=15, performance=4, commit_quality=3))  # noqa: E501
    forge = FakeForge()
    # 极短退避避免拖慢测试
    writer = ResultWriter(forge, backoff_base=0.001, backoff_max=0.01)  # type: ignore[arg-type]
    asyncio.run(writer.write(_pr(), [], result))

    assert forge.summary_attempts == 3  # 首 + 2 次重试


def test_writer_gives_up_after_exhausted_retries_with_readable_error():
    """连接持续失败 → 重试耗尽后抛可读错误（带底层信息），而非裸空消息。"""
    class FakeForge:
        async def post_inline(self, pr, comments: list[dict]) -> None:
            pass

        async def post_summary(self, pr, body: str) -> None:
            raise httpx.ConnectError("") from OSError("Connection refused")

    writer = ResultWriter(FakeForge(), retries=1, backoff_base=0.001, backoff_max=0.01)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(writer.write(_pr(), [], ReviewResult(
            summary="s", scores=ReviewScores(correctness=1, security=1, practices=1, performance=1, commit_quality=1),
        )))
    # 文案补上底层连接原因，排障可读
    assert "Connection refused" in str(exc.value)
    assert "总结评论" in str(exc.value)


def test_writer_does_not_retry_http_status_error():
    """HTTPStatusError（平台真实拒绝，如 403/422）不重试，只调一次原样上抛。"""
    class FakeForge:
        def __init__(self) -> None:
            self.summary_attempts = 0

        async def post_inline(self, pr, comments: list[dict]) -> None:
            pass

        async def post_summary(self, pr, body: str) -> None:
            self.summary_attempts += 1
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    forge = FakeForge()
    writer = ResultWriter(forge, retries=5, backoff_base=0.001, backoff_max=0.01)  # type: ignore[arg-type]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(writer.write(_pr(), [], ReviewResult(
            summary="s", scores=ReviewScores(correctness=1, security=1, practices=1, performance=1, commit_quality=1),
        )))
    assert forge.summary_attempts == 1


# ── 评论指纹幂等兜底（方案 3）─────────────────────────────────────────────


def test_writer_skips_when_fingerprint_already_delivered():
    """平台已含指纹 → 整体跳过，不再发行级/总结评论（幂等，重发不双发）。"""
    class FakeForge:
        def __init__(self) -> None:
            self.calls = 0

        async def list_comments(self, pr) -> list[str]:
            return ["已有评论", "<!-- fp123 -->"]

        async def post_inline(self, pr, comments: list[dict]) -> None:
            self.calls += 1

        async def post_summary(self, pr, body: str) -> None:
            self.calls += 1

    forge = FakeForge()
    writer = ResultWriter(forge)  # type: ignore[arg-type]
    asyncio.run(writer.write(_pr(), [_diff("a.py", DIFF)],
                             ReviewResult(summary="s", findings=[_finding(line=2)]),
                             fingerprint="fp123"))
    assert forge.calls == 0  # 幂等命中，零回写


def test_writer_appends_fingerprint_when_not_delivered():
    """平台未含指纹 → 照常回写，且总结末尾带上指纹哨兵。"""
    class FakeForge:
        def __init__(self) -> None:
            self.summary_bodies: list[str] = []

        async def list_comments(self, pr) -> list[str]:
            return ["无关评论"]

        async def post_inline(self, pr, comments: list[dict]) -> None:
            pass

        async def post_summary(self, pr, body: str) -> None:
            self.summary_bodies.append(body)

    forge = FakeForge()
    writer = ResultWriter(forge)  # type: ignore[arg-type]
    asyncio.run(writer.write(_pr(), [], ReviewResult(summary="s"), fingerprint="fp456"))
    assert forge.summary_bodies[0].endswith("<!-- fp456 -->")


def test_writer_degrades_when_forge_lacks_list_comments():
    """无 list_comments 的极简/测试桩 forge → 无法幂等去重，照发（不崩）。"""
    class FakeForge:
        async def post_inline(self, pr, comments: list[dict]) -> None:
            pass

        async def post_summary(self, pr, body: str) -> None:
            pass

    writer = ResultWriter(FakeForge())  # type: ignore[arg-type]
    # 不应抛（list_comments 缺失被守卫），照常写完
    asyncio.run(writer.write(_pr(), [], ReviewResult(summary="s"), fingerprint="fp789"))


def test_review_fingerprint_is_stable_across_calls():
    """同 (provider, repo_id, pr, head_sha) 指纹稳定，重发幂等去重依赖它。"""
    a = review_fingerprint(provider="github", repo_id="o/r", pr_number=1, head_sha="h")
    b = review_fingerprint(provider="github", repo_id="o/r", pr_number=1, head_sha="h")
    c = review_fingerprint(provider="github", repo_id="o/r", pr_number=1, head_sha="other")
    assert a == b and len(a) == 16
    assert a != c  # head 变化 → 指纹不同


# ── redeliver：从持久化成果补发，不重算 ──────────────────────────────────


def test_redeliver_uses_persisted_findings_and_summary():
    """重发走 fetch + ResultWriter：带持久化 summary 覆盖、指纹幂等，不调 LLM。"""
    class FakeForge:
        def __init__(self) -> None:
            self.inline_bodies: list[str] = []
            self.summary_bodies: list[str] = []
            self.fetched = 0

        async def fetch_pull_request(self, pr) -> PullRequest:
            self.fetched += 1
            return pr

        async def fetch_files(self, pr) -> list[FileDiff]:
            return [_diff("a.py", DIFF)]

        async def list_comments(self, pr) -> list[str]:
            return []  # 未投递 → 会发

        async def post_inline(self, pr, comments: list[dict]) -> None:
            self.inline_bodies.extend(c["body"] for c in comments)

        async def post_summary(self, pr, body: str) -> None:
            self.summary_bodies.append(body)

    old = _finding(line=2, content="持久化的问题")
    forge = FakeForge()
    asyncio.run(redeliver(
        forge,  # type: ignore[arg-type]
        pr=_pr(), findings=[old], summary_md="已持久化总结", fingerprint="fp000",
    ))

    assert forge.fetched == 1  # 重取了一次 diff
    assert any("持久化的问题" in b for b in forge.inline_bodies)  # findings 从 DB 取回
    assert "已持久化总结" in forge.summary_bodies[0]  # summary 用持久化值覆盖，不重算
    assert forge.summary_bodies[0].endswith("<!-- fp000 -->")

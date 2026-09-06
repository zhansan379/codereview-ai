"""review/result_writer 测试：findings 分区、评论 dict 转换、总结 Markdown。

全程离线：forge 用内存 fake，只断言调用而非真实网络。
"""

from __future__ import annotations

import asyncio

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

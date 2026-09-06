"""语义分组接入主链测试（DESIGN §7.5 / M4.6）：大变更分组并发 + 确定性合并。

离线：fake GroupLLM 把 message_en/zh 两文件并成一组、其余 per-file；fake Reviewer 每文件
返回一个 finding（含重复指纹），断言各组各审一次、结果合并、跨组指纹去重。
"""

from __future__ import annotations

from codereview_ai.domain.models import (  # noqa: E501
    Category,
    ChangeType,
    FileDiff,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
    Severity,
)
from codereview_ai.review.group_review import GROUPING_MIN_FILES, merge_results, review_in_groups
from codereview_ai.review.grouping import SemanticGrouper


def _diff(path: str) -> FileDiff:
    return FileDiff(old_path=path, new_path=path, diff=f"@@ -0,0 +1 @@\n+{path}\n",
                    additions=1, deletions=0, change_type=ChangeType.NEW_FILE)


def _pr(head: str = "h") -> PullRequest:
    return PullRequest(provider="github", repo_id="9", repo_full_name="a/b", web_url="u",
                       pr_number=1, title="t", source_branch="f", target_branch="m",
                       head_sha=head, base_sha="b")


class FakeGroupLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def group_metadata(self, entries: list[str], max_files: int) -> list[list[str]]:
        self.calls += 1
        paths = [e.split("  ")[1].split(" (")[0] for e in entries]
        groups = [p for p in paths]
        # 模拟把 message_en / message_zh 并入一组
        if {"messages_en.py", "messages_zh.py"} <= set(paths):
            merged = [p for p in paths if p in ("messages_en.py", "messages_zh.py")]
            rest = [p for p in paths if p not in ("messages_en.py", "messages_zh.py")]
            return [merged] + [[p] for p in rest]
        return [[p] for p in groups]


class FakeReviewer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def review(self, *, pr, commits_text, diffs) -> ReviewResult:
        paths = sorted(d.new_path for d in diffs)
        self.calls.append(",".join(paths))
        r = ReviewResult(summary=f"summary[{paths[0]}]")
        r.scores = ReviewScores(correctness=1 if "messages" in paths[0] else 5,
                                security=0, practices=0, performance=0, commit_quality=0)
        for p in paths:
            # 两个 message 文件报同一内容 → 指纹应去重
            r.findings.append(Finding(content="duplicate issue", category=Category.BUG,
                                      severity=Severity.HIGH, existing_code="x", file=p))
        return r


def _big_diffs() -> list[FileDiff]:
    return [_diff(p) for p in (
        "messages_en.py", "messages_zh.py", "main.py", "utils.py", "cli.py",
    )]


def test_merge_results_unions_dedups_and_keeps_worst_score():
    def finding(path: str, content: str) -> Finding:
        return Finding(content=content, category=Category.BUG, severity=Severity.HIGH,
                       existing_code="x", file=path)

    r1 = ReviewResult(summary="s1")
    r1.scores = ReviewScores(correctness=1)  # 低
    r1.findings = [finding("a.py", "dup"), finding("b.py", "ok")]

    r2 = ReviewResult(summary="s2")
    r2.scores = ReviewScores(correctness=9)  # 高
    r2.findings = [finding("a.py", "dup")]  # 与 r1 同文件同内容 → 去重

    merged = merge_results([r1, r2], [[_diff("a.py"), _diff("b.py")], [_diff("a.py")]])
    # union 后跨组去重：a.py 的 dup 只留一条；b.py 一条
    assert [f.file for f in merged.findings] == ["a.py", "b.py"]
    # 逐维取最坏值：correctness 取 max(1,9)=9
    assert merged.scores.correctness == 9
    assert merged.summary


async def test_review_in_groups_small_change_single_call():
    reviewer = FakeReviewer()
    grouper = SemanticGrouper(FakeGroupLLM())
    diffs = [_diff("only.py")]  # < GROUPING_MIN_FILES
    result = await review_in_groups(reviewer, grouper, _pr(), "t", diffs)
    assert reviewer.calls == ["only.py"]  # 小变更 → 整组一次
    assert len(result.findings) == 1


async def test_review_in_groups_large_change_groups_and_merges():
    reviewer = FakeReviewer()
    grouper = SemanticGrouper(FakeGroupLLM())
    diffs = _big_diffs()
    assert len(diffs) >= GROUPING_MIN_FILES
    result = await review_in_groups(reviewer, grouper, _pr(), "t", diffs)
    # 分组：[messages_en,messages_zh]，main，utils，cli → 4 次独立审查
    assert len(reviewer.calls) == 4
    assert "messages_en.py,messages_zh.py" in reviewer.calls
    # 每组各产出一条（每文件去重后各一）；5 组文件 → 5 条
    assert len(result.findings) == 5
    # 最坏 correctness（utils 组的 5）
    assert result.scores.correctness == 5

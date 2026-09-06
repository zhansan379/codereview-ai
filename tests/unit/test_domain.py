"""领域模型测试：枚举归一、dataclass 默认值、ReviewScores 汇总。"""

from __future__ import annotations

from datetime import UTC

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


def test_change_type_string_values():
    assert ChangeType.NEW_FILE.value == "new"
    assert ChangeType.MODIFIED.value == "modified"


def test_enums_coerce_from_string():
    # str 子类枚举可直接比较，且能由字符串构造
    assert Severity("critical") == Severity.CRITICAL
    assert Category("security") == Category.SECURITY


def test_filediff_frozen_and_defaults():
    d = FileDiff(
        old_path="a.py", new_path="a.py", diff="@@ -1 +1 @@",
        additions=1, deletions=0, change_type=ChangeType.MODIFIED,
    )
    assert d.new_file_content == ""  # 默认值
    try:
        d.diff = "mutate"
        raise AssertionError("expected FrozenInstanceError")
    except Exception as exc:
        import dataclasses

        assert isinstance(exc, dataclasses.FrozenInstanceError)


def test_pullrequest_construction():
    pr = PullRequest(
        provider="gitlab", repo_id=1, repo_full_name="o/r", web_url="http://w",
        pr_number=3, title="t", source_branch="f", target_branch="main",
        head_sha="abc", base_sha="def",
    )
    assert pr.is_draft is False
    assert pr.diff_refs is None


def test_finding_defaults():
    f = Finding(
        content="x", category=Category.BUG, severity=Severity.HIGH,
        existing_code="return 0;", file="a.py",
    )
    assert f.line is None
    assert f.source == "llm"
    assert f.suggestion_code is None


def test_review_scores_total():
    s = ReviewScores(correctness=3, security=4, practices=2, performance=1, commit_quality=5)
    assert s.total == 15
    assert ReviewScores().total == 0


def test_review_result_defaults():
    r = ReviewResult()
    assert r.findings == []
    assert r.skipped_files == []
    assert r.scores.total == 0


def test_commit_timestamp_is_utc_aware():
    from codereview_ai.domain.models import CommitInfo

    c = CommitInfo(sha="abc", message="init")
    assert c.timestamp.tzinfo is UTC

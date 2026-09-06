"""review/increments 测试：增量决定 + 内容指纹去重（DESIGN §7.3）。

纯离线：无网络、无 DB，只测纯函数与内存态 store。
"""

from __future__ import annotations

from codereview_ai.domain.models import Category, Finding, PullRequest, Severity
from codereview_ai.review.increments import (
    REASON_ALREADY,
    REASON_CHAIN_INVALID,
    REASON_FIRST,
    REASON_INCREMENTAL,
    IncrementStore,
    collect_fingerprints,
    decide_increment,
    dedup_findings,
    finding_fingerprint,
)


def _pr(head: str = "h2", number: int = 7) -> PullRequest:
    return PullRequest(
        provider="gitlab", repo_id="7", repo_full_name="acme/widgets", web_url="",
        pr_number=number, title="t", source_branch="s", target_branch="main",
        head_sha=head, base_sha="b",
    )


def _finding(file: str, content: str) -> Finding:
    return Finding(
        content=content, category=Category.BUG, severity=Severity.HIGH,
        existing_code="x", file=file,
    )


# ── decide_increment ─────────────────────────────────────────────────────


def test_first_review_is_full():
    s = IncrementStore()
    d = decide_increment(s, _pr(), chain_valid=True)
    assert d.is_incremental is False
    assert d.reason == REASON_FIRST
    assert d.last_reviewed_sha is None


def test_same_head_skips():
    s = IncrementStore()
    s.record("gitlab", 7, "h2")
    d = decide_increment(s, _pr(head="h2"), chain_valid=True)
    assert d.is_incremental is False
    assert d.reason == REASON_ALREADY


def test_new_head_on_valid_chain_is_incremental():
    s = IncrementStore()
    s.record("gitlab", 7, "h1")
    d = decide_increment(s, _pr(head="h2"), chain_valid=True)
    assert d.is_incremental is True
    assert d.reason == REASON_INCREMENTAL
    assert d.last_reviewed_sha == "h1"


def test_new_head_invalid_chain_falls_back_full():
    # force-push/rebase → 上次 head 脱离链 → 回退全量（保守门，DESIGN §7.3）
    s = IncrementStore()
    s.record("gitlab", 7, "h1")
    d = decide_increment(s, _pr(head="h2"), chain_valid=False)
    assert d.is_incremental is False
    assert d.reason == REASON_CHAIN_INVALID
    assert d.last_reviewed_sha == "h1"


def test_store_is_scoped_per_provider_and_pr():
    s = IncrementStore()
    s.record("gitlab", 7, "h1")
    s.record("github", 7, "hg")
    assert s.last("gitlab", 7).head_sha == "h1"
    assert s.last("github", 7).head_sha == "hg"
    assert s.last("gitlab", 99) is None


# ── 内容指纹去重 ─────────────────────────────────────────────────────────


def test_fingerprint_ignores_case_and_whitespace():
    a = finding_fingerprint(_finding("a.py", "  UseCamelCase  "))
    b = finding_fingerprint(_finding("a.py", "usecamelcase"))
    assert a == b


def test_fingerprint_distinguishes_file():
    assert finding_fingerprint(_finding("a.py", "x")) != finding_fingerprint(_finding("b.py", "x"))  # noqa: E501


def test_dedup_removes_prior_findings():
    repeat = _finding("a.py", "重复问题")
    fresh = _finding("b.py", "新问题")
    s = IncrementStore()
    s.record("gitlab", 7, "h1", collect_fingerprints([repeat]))
    kept = dedup_findings([repeat, fresh], s.last("gitlab", 7))
    assert kept == [fresh]


def test_dedup_without_ref_is_identity():
    f = _finding("a.py", "x")
    assert dedup_findings([f], None) == [f]


def test_dedup_no_stored_fingerprints_keeps_all():
    # 上次没存指纹（兼容）→ 保守不删
    f = _finding("a.py", "x")
    s = IncrementStore()
    s.record("gitlab", 7, "h1")
    assert dedup_findings([f], s.last("gitlab", 7)) == [f]


def test_collect_fingerprints_dedupes_equal_bodies():
    f1 = _finding("a.py", "同字")
    f2 = _finding("a.py", "  同字 ")
    s = collect_fingerprints([f1, f2])
    assert len(s) == 1

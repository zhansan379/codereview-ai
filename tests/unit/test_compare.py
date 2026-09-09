"""离线单测：前后两次审查四桶增量对比（`bucket_compare` 纯函数）。

对齐 OCR `Compare` 语义：new / persisting / resolved / not_reviewed。
- 按指纹 multiset 差：前有后无且文件在覆盖集 → resolved，不在 → not_reviewed。
- 不做行号匹配、不碰被 `reconcile_findings` 状态机改过的 `status`。
"""

from __future__ import annotations

from codereview_ai.domain.models import Category, Finding, Severity
from codereview_ai.review.compare import bucket_compare


def _f(content: str, file: str = "a.py", line: int | None = None) -> Finding:
    return Finding(
        content=content, category=Category.BUG, severity=Severity.HIGH,
        existing_code="", file=file, line=line, source="agent",
    )


def _contents(bucket):
    return [x["content"] for x in bucket]


def test_four_bucket_split():
    before = [_f("c0"), _f("c1")]  # c1 本轮消失
    after = [_f("c0"), _f("c2")]  # c2 本轮新增
    res = bucket_compare(before, after, after_covered={"a.py"})
    assert _contents(res.persisting) == ["c0"]
    assert _contents(res.new) == ["c2"]
    assert _contents(res.resolved) == ["c1"]  # a.py 覆盖到 → 已解决
    assert _contents(res.not_reviewed) == []


def test_not_reviewed_when_file_not_covered():
    # c2 在 b.py，本轮没审到 b.py → 不算已解决 → not_reviewed
    before = [_f("c0", "a.py"), _f("c2", "b.py")]
    after = [_f("c0", "a.py")]
    res = bucket_compare(before, after, after_covered={"a.py"})
    assert _contents(res.resolved) == []
    assert _contents(res.not_reviewed) == ["c2"]  # 文件没覆盖，保守登记


def test_multiset_duplicate_fingerprint():
    # 同指纹 3 份对 1 份：1 份 persisting，2 份 resolved
    before = [_f("dup")] * 3
    after = [_f("dup")]
    res = bucket_compare(before, after, after_covered={"a.py"})
    assert len(res.persisting) == 1
    assert len(res.resolved) == 2
    assert res.new == []


def test_everything_new_when_no_before():
    after = [_f("n1"), _f("n2")]
    res = bucket_compare([], after, after_covered={"a.py"})
    assert _contents(res.new) == ["n1", "n2"]
    assert res.persisting == [] and res.resolved == [] and res.not_reviewed == []


def test_empty_both_sides():
    res = bucket_compare([], [], set())
    assert res.new == [] and res.persisting == [] and res.resolved == [] and res.not_reviewed == []


def test_flat_carries_line_and_old_line():
    b = _f("c0", line=10)
    res = bucket_compare([], [b], {"a.py"})
    row = res.new[0]
    assert row["line"] == 10 and row["file"] == "a.py"
    assert row["severity"] == "high" and row["category"] == "bug"


def test_buckets_sorted_stable():
    # 桶内按 file → line → category → content 稳定排序，不依赖插入序（对齐 OCR sortFindings）。
    after = [
        _f("zz", "b.py", line=5),
        _f("aa", "a.py", line=1),
        _f("mm", "a.py", line=2),
    ]
    res = bucket_compare([], after, {"a.py", "b.py"})
    assert [x["file"] for x in res.new] == ["a.py", "a.py", "b.py"]
    assert [x["line"] for x in res.new] == [1, 2, 5]

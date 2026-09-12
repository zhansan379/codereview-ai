"""离线单测：PR 级聚合收敛（`group_pr_deltas` 纯函数）。

对齐 `bucket_compare` 语义的聚合：同一 PR 多轮已完成审查串成时间线，
相邻轮做四桶差量；首轮 all-new；末轮带完整四桶 + 收敛率。
"""

from __future__ import annotations

from codereview_ai.review.pr_compare import PrDelta, group_pr_deltas


def _f(content: str, file: str = "a.py") -> object:
    """已 `_FindRow` 适配的最小行：只要 bucket_compare/指纹要读的字段。"""
    return type("Row", (), {
        "file": file, "content": content, "category": "bug", "severity": "high",
        "line": 1, "old_line": None, "existing_code": "", "source": "agent",
    })()


def _delta_counts(p: PrDelta) -> list[dict[str, int]]:
    return [r.delta for r in p.rounds]


def test_first_round_all_new():
    # 首轮：before=[]，全部进 new。
    rows = [(1, "sha1", [_f("c0"), _f("c1")], {"a.py"})]
    prs = group_pr_deltas({("gh", "repo", 1): rows})
    assert len(prs) == 1
    assert _delta_counts(prs[0]) == [{"new": 2, "persisting": 0, "resolved": 0, "not_reviewed": 0}]
    assert set(prs[0].last_delta) == {"new", "persisting", "resolved", "not_reviewed"}
    assert len(prs[0].last_delta["new"]) == 2


def test_adjacent_round_delta():
    # 3 轮：c0 全程、c1 第二轮消失(已解决)、c2 第二轮新冒、c3 第三轮新冒。
    r1 = [_f("c0")]
    r2 = [_f("c0"), _f("c2")]  # 相对 r1：+new c2
    r3 = [_f("c0"), _f("c3")]  # 相对 r2：c2 resolve、c3 new
    rows = [(1, "sha1", r1, {"a.py"}), (2, "sha2", r2, {"a.py"}), (3, "sha3", r3, {"a.py"})]
    prs = group_pr_deltas({("gh", "repo", 1): rows})
    assert _delta_counts(prs[0]) == [
        {"new": 1, "persisting": 0, "resolved": 0, "not_reviewed": 0},
        {"new": 1, "persisting": 1, "resolved": 0, "not_reviewed": 0},
        {"new": 1, "persisting": 1, "resolved": 1, "not_reviewed": 0},
    ]
    # 末轮四桶 = 第 3 轮差量；resolved 含 c2。
    last = prs[0].last_delta
    assert [x["content"] for x in last["resolved"]] == ["c2"]
    assert [x["content"] for x in last["new"]] == ["c3"]
    assert len(last["persisting"]) == 1


def test_rounds_by_task_id_order_respected():
    # 入参按 id 升序；聚合不做排序假设——乱序喂入应仍按给出顺序处理（调用方保证升序）。
    rows = [(1, "sha1", [_f("c0")], {"a.py"}), (2, "sha2", [_f("c0"), _f("c2")], {"a.py"})]
    prs = group_pr_deltas({("gh", "repo", 7): rows})
    assert [r.id for r in prs[0].rounds] == [1, 2]


def test_rate_pct():
    # 末轮：r2 相对 r1 持续 1（A）· 已解决 4（B–E）。
    # 速率 = resolved/(resolved+persisting+not_reviewed) = 4/(4+1+0) → 80。
    rows = [(1, "sha1", [_f("A"), _f("B"), _f("C"), _f("D"), _f("E")], {"a.py"}),
            (2, "sha2", [_f("A")], {"a.py"})]
    prs = group_pr_deltas({("gh", "repo", 1): rows})
    assert prs[0].rate_pct == 80


def test_single_round_rate_full():
    # 仅一轮：无历史可收敛 → 定义域内无“该收敛的”，速率=100（前端单轮时显示“—”）。
    rows = [(1, "sha1", [_f("c0"), _f("c1")], {"a.py"})]
    prs = group_pr_deltas({("gh", "repo", 1): rows})
    assert prs[0].rate_pct == 100
    assert len(prs[0].rounds) == 1


def test_multiple_prs_key_isolated():
    rows_a = [(1, "sha1", [_f("c0")], {"a.py"})]
    rows_b = [(5, "sha5", [_f("z0")], {"b.py"})]
    prs = group_pr_deltas({
        ("gh", "repo", 1): rows_a,
        ("gl", "other", 2): rows_b,
    })
    assert [p.key for p in prs] == ["gh:repo:1", "gl:other:2"]

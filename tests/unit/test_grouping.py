"""review/grouping 测试：LLM 语义分组决策链与降级（DESIGN §7.2）。

纯离线：LLM 用 fake `GroupLLM` 注入，不触网络。
"""

from __future__ import annotations

import asyncio

from codereview_ai.domain.models import ChangeType, FileDiff
from codereview_ai.review.grouping import (
    SemanticGrouper,
    decide_grouping,
    diff_churn,
    format_diff_entry,
)

MOD = ChangeType.MODIFIED


def _d(path: str, add: int = 10, dele: int = 5, ct: ChangeType = MOD) -> FileDiff:
    return FileDiff(old_path=path, new_path=path, diff="", additions=add, deletions=dele, change_type=ct)  # noqa: E501


class _FakeGroupLLM:
    """可编程 fake：按配置返回分组/None/异常。"""

    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[list[str]] = []

    async def group_metadata(self, entries: list[str], max_files: int) -> list[list[str]] | None:  # noqa: E501
        self.calls.append(entries)
        if self._exc is not None:
            raise self._exc
        return self._result


def test_format_diff_entry_renders_metadata():
    d = _d("a.py", add=12, dele=3)
    assert format_diff_entry(d) == "M  a.py (+12/-3)"


def test_format_diff_entry_status_letters():
    assert format_diff_entry(_d("a", ct=ChangeType.NEW_FILE)).startswith("A  ")
    assert format_diff_entry(_d("a", ct=ChangeType.DELETED_FILE)).startswith("D  ")
    assert format_diff_entry(_d("a", ct=ChangeType.RENAMED_FILE)).startswith("R  ")


def test_churn_sums_additions_deletions():
    assert diff_churn([_d("a", 10, 5), _d("b", 2, 0)]) == 17


def test_len_one_is_single_group():
    assert decide_grouping([_d("a.py")]) == [[_d("a.py")]]


def test_small_change_bundles_all():
    diffs = [_d("a.py", 30, 0), _d("b.py", 20, 0), _d("c.py", 10, 0)]
    assert decide_grouping(diffs) == [diffs]


def test_small_change_but_high_churn_not_bundled():
    # files<4 但 churn>=200 → 不走整包，降级 per-file
    diffs = [_d("a.py", 150, 0), _d("b.py", 150, 0)]
    g = decide_grouping(diffs)
    assert g == [[diffs[0]], [diffs[1]]]


def test_many_files_no_llm_falls_back_per_file():
    diffs = [_d(f"f{n}.py", 30, 0) for n in range(6)]
    g = decide_grouping(diffs)
    assert g == [[d] for d in diffs]


def test_semantic_llm_groups_related_files():
    # 4 个文件（≥4 不走小改动短路）→ LLM 可把关联文件并进一组
    llm = _FakeGroupLLM(result=[["msg_en.properties", "msg_zh.properties"], ["app.py"], ["util.py"]])  # noqa: E501
    diffs = [_d("msg_en.properties", 5, 0), _d("msg_zh.properties", 5, 0), _d("app.py", 5, 0), _d("util.py", 5, 0)]  # noqa: E501
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group(diffs))
    # 关联文件并进一组；调用方拿到元数据行
    names = [[d.new_path for d in grp] for grp in out]
    assert names == [["msg_en.properties", "msg_zh.properties"], ["app.py"], ["util.py"]]
    assert len(llm.calls) == 1
    assert all(e.startswith("M  ") and "(+" in e for e in llm.calls[0])


def test_semantic_llm_none_falls_back_per_file():
    llm = _FakeGroupLLM(result=None)
    diffs = [_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py")]
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group(diffs))
    assert out == [[d] for d in diffs]


def test_semantic_llm_exception_falls_back_per_file():
    llm = _FakeGroupLLM(exc=RuntimeError("分组失败"))
    diffs = [_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py")]
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group(diffs))
    assert out == [[d] for d in diffs]


def test_semantic_llm_per_fileish_result_ignored():
    # LLM 返回每组才一个文件（不实际并组）→ 视作 per-file，等价即可
    llm = _FakeGroupLLM(result=[["a.py"], ["b.py"], ["c.py"], ["d.py"]])
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group([_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py")]))
    assert len(out) == 4


def test_oversized_group_split_by_max_files():
    llm = _FakeGroupLLM(result=[["a.py"], ["b.py"], ["c.py"]])
    g = SemanticGrouper(llm, max_files=1)
    out = asyncio.run(g.group([_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py"), _d("e.py")]))
    # 每组至多 1 文件
    assert all(len(grp) <= 1 for grp in out)


def test_budget_overrun_splits_to_single_groups():
    # 超大 churn 文件混入 → 预算拆分，单文件各自成组
    big = _d("big.py", add=3000, dele=0)
    diffs = [big, _d("a.py", 5, 0), _d("b.py", 5, 0)]
    g = SemanticGrouper(_FakeGroupLLM(result=None), group_churn_budget=100)
    out = asyncio.run(g.group(diffs))
    flat = [d.new_path for grp in out for d in grp]
    assert set(flat) == {"big.py", "a.py", "b.py"}
    # big 独立成组；小文件各自成组
    assert all(d.additions + d.deletions <= 100 or len(grp) == 1 for grp in out for d in grp)


def test_semantic_not_triggered_for_small_change():
    llm = _FakeGroupLLM(result=[["same"], ["x"]])
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group([_d("a.py", 1, 0), _d("b.py", 1, 0)]))
    assert out == [[_d("a.py", 1, 0), _d("b.py", 1, 0)]]  # 整包，未走 LLM
    assert llm.calls == []

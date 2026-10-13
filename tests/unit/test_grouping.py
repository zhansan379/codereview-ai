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


def _d(path: str, add: int = 10, dele: int = 5, ct: ChangeType = MOD,
       diff: str = "") -> FileDiff:
    return FileDiff(old_path=path, new_path=path, diff=diff, additions=add,
                    deletions=dele, change_type=ct)


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


def test_semantic_llm_per_file_result_kept():
    # OCR 行为：LLM 判定各文件互不相关（每组一个文件）时**照用**，不改写成装箱。
    # 旧实现用 _looks_like_per_file 把这种结果判为「不值得当 LLM 结果」并重新装箱，
    # 会让多个文件共享同一份会话预算（轮数/prompt），召回明显下降——已按 OCR 移除。
    llm = _FakeGroupLLM(result=[["a.py"], ["b.py"], ["c.py"], ["d.py"]])
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group([_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py")]))
    assert len(out) == 4


def test_semantic_llm_failure_falls_back_to_per_file():
    # OCR grouping.go:98：分组 LLM 失败/空响应 → **per-file**（不做任何装箱）
    llm = _FakeGroupLLM(exc=RuntimeError("LLM returned empty content"))
    diffs = [_d(f"f{n}.py", 30, 0) for n in range(25)]
    out = asyncio.run(SemanticGrouper(llm).group(diffs))
    assert len(out) == len(diffs)
    assert all(len(grp) == 1 for grp in out)
    assert {d.new_path for grp in out for d in grp} == {f"f{n}.py" for n in range(25)}


def test_semantic_llm_failure_retries_once_then_succeeds():
    # 首次空响应/异常 → 重试一次；成功则走 LLM 分组结果（不再降级）
    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def group_metadata(self, entries, max_files):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("LLM returned empty content")
            return [["a.py", "b.py"], ["c.py"], ["d.py"]]

    llm = _Flaky()
    diffs = [_d("a.py", 5, 0), _d("b.py", 5, 0), _d("c.py", 5, 0), _d("d.py", 5, 0)]
    out = asyncio.run(SemanticGrouper(llm).group(diffs))
    assert llm.calls == 2
    assert [d.new_path for d in out[0]] == ["a.py", "b.py"]


def test_oversized_group_split_by_max_files():
    llm = _FakeGroupLLM(result=[["a.py"], ["b.py"], ["c.py"]])
    g = SemanticGrouper(llm, max_files=1)
    out = asyncio.run(g.group([_d("a.py"), _d("b.py"), _d("c.py"), _d("d.py"), _d("e.py")]))
    # 每组至多 1 文件
    assert all(len(grp) <= 1 for grp in out)


def test_group_token_budget_splits_to_single_groups():
    # OCR enforceGroupTokenBudget：组内 diff 累计 token 超预算 → 整组拆成 per-file。
    # 预算按 diff 文本估（chars//4，与 llmloop.estimate_tokens 同口径）。
    big = _d("big.py", 3000, 0, diff="x" * 4000)     # ~1000 token
    small = _d("a.py", 5, 0, diff="x" * 40)
    llm = _FakeGroupLLM(result=[["big.py", "a.py"]])  # 故意把两者并成一组
    g = SemanticGrouper(llm, group_token_budget=500)
    out = asyncio.run(g.group([big, small]))
    assert sorted(len(grp) for grp in out) == [1, 1]
    assert {d.new_path for grp in out for d in grp} == {"big.py", "a.py"}


def test_semantic_not_triggered_for_small_change():
    llm = _FakeGroupLLM(result=[["same"], ["x"]])
    g = SemanticGrouper(llm)
    out = asyncio.run(g.group([_d("a.py", 1, 0), _d("b.py", 1, 0)]))
    assert out == [[_d("a.py", 1, 0), _d("b.py", 1, 0)]]  # 整包，未走 LLM
    assert llm.calls == []

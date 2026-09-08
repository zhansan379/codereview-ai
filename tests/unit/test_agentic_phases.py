"""离线单测：OCR 阶段机新阶段（plan / re_location / review_filter / scoring）+ 集成。

- planner：PlanRequired 门控阈值、plan 阶段返回/落空、空 plan 剥块。
- relocation：确定性本文件匹配填 line、跨文件唯一串搜重锚、LLM 重定位（fenced block→重试命中）、
  失败还原。
- review_filter：report_incorrect_comments 删对应 c-N、approve_all 全留、无调用全留、越界 id 忽略。
- scoring：fake 返回 JSON → 填对 ReviewScores；坏 JSON/失败归 0（不降级）。
- 集成：脚本化 fake 依次驱动 plan→main→re_location→filter→score，result.scores 有值、
  findings 行定位进可评论集。
"""

from __future__ import annotations

import json

from codereview_ai.domain.models import (
    Category,
    ChangeType,
    FileDiff,
    Finding,
    Severity,
)
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentTurn, ToolCall
from codereview_ai.review.agentic.planner import (
    plan_required,
    run_plan_phase,
)
from codereview_ai.review.agentic.prompt_builder import build_main_task_intro
from codereview_ai.review.agentic.relocation import (
    resolve_leaked_lines,
)
from codereview_ai.review.agentic.review_filter import (
    parse_filter_result,
    run_review_filter,
)
from codereview_ai.review.agentic.sandbox import FakeRuntime, run_agentic_review
from codereview_ai.review.agentic.scoring import (
    _parse_scores,
    run_scoring,
)


def _fd(path="a.py", content="x = 1\n", diff=None, additions=1) -> FileDiff:
    diff = diff if diff is not None else f"+ {content}"
    return FileDiff(path, path, diff, additions, 0, ChangeType.MODIFIED, content)


def _finding(**kw) -> Finding:
    defaults = dict(
        content="问题描述", category=Category.BUG, severity=Severity.HIGH,
        file="a.py", existing_code="x = 1", line=None, side=None, source="agent",
    )
    defaults.update(kw)
    return Finding(**defaults)


# ── planner ────────────────────────────────────────────────────────────


def test_plan_required_single_large_file():
    small = [_fd(content="a\n")]
    assert plan_required(small, line_threshold=300, group_line_threshold=600) is False
    big = [FileDiff("a.py", "a.py", "+ x" * 400, 400, 0, ChangeType.MODIFIED, "x" * 400)]
    assert plan_required(big, line_threshold=300, group_line_threshold=600) is True


def test_plan_required_group_churn():
    # 多文件组 churn 累计超 group 阈值才规划（churn = additions + deletions）
    two = [_fd("a.py", additions=310), _fd("b.py", additions=310)]
    assert plan_required(two, line_threshold=300, group_line_threshold=600) is True
    small_group = [_fd("a.py", additions=200), _fd("b.py", additions=200)]
    assert plan_required(small_group, line_threshold=300, group_line_threshold=600) is False


class _PlanLLM:
    def __init__(self, text: str, fail: bool = False) -> None:
        self._text = text
        self._fail = fail

    async def chat(self, _messages, _tools) -> AgentTurn:  # noqa: ANN001
        if self._fail:
            raise RuntimeError("plan boom")
        return AgentTurn(content=self._text)

    async def summarize(self, _f, _c) -> str:
        return "s"


async def test_plan_phase_returns_text_and_empty_on_failure():
    empty_msg = [{"role": "u", "content": ""}]
    got = await run_plan_phase(_PlanLLM("Summary: ...\nIssues\n(none)"), empty_msg)
    assert got.startswith("Summary:")
    assert await run_plan_phase(_PlanLLM(""), empty_msg) == ""
    assert await run_plan_phase(_PlanLLM("ignored", fail=True), empty_msg) == ""


def test_main_intro_strips_empty_plan_block():
    with_plan = build_main_task_intro(diffs=[_fd()], plan="Summary: p\n")
    assert "### Review Plan" in with_plan and "Summary: p" in with_plan
    no_plan = build_main_task_intro(diffs=[_fd()], plan="")
    assert "### Review Plan" not in no_plan and "{{plan_guidance}}" not in no_plan


# ── relocation 钉行阶梯 ─────────────────────────────────────────────────


def test_relocation_deterministic_match_fills_line():
    f = _finding()  # no line, existing_code = "x = 1"
    diff_map = {"a.py": _fd(content="x = 1\ny = 2\n")}
    comments = asyncio_run(resolve_leaked_lines([f], diff_map, _PlanLLM(""), AgentConfig()))
    assert comments[0].line == 1
    assert comments[0].side == "RIGHT"


def test_relocation_across_files_unique_anchor():
    f = _finding(file="a.py", existing_code="SENTINEL_ONLY")
    # SENTINEL_ONLY 只在 b.py 新内容出现 → 跨文件唯一命中，重钉到 b.py
    diff_map = {
        "a.py": _fd(content="x = 1\n"),
        "b.py": _fd("b.py", content="z = 2\nSENTINEL_ONLY\n"),
    }
    comments = asyncio_run(resolve_leaked_lines([f], diff_map, _PlanLLM(""), AgentConfig()))
    assert comments[0].file == "b.py"
    assert comments[0].line == 2


def test_relocation_ambiguous_across_files_rejected():
    f = _finding(file="a.py", existing_code="DUP")
    # DUP 不在 a.py，但在 b.py 和 c.py 都有 → 跨文件多命中拒绝重锚，保持原 file 且不猜行
    diff_map = {
        "a.py": _fd(content="x = 1\n"),
        "b.py": _fd("b.py", content="DUP\n"),
        "c.py": _fd("c.py", content="DUP\n"),
    }
    comments = asyncio_run(resolve_leaked_lines([f], diff_map, _PlanLLM(""), AgentConfig()))
    assert comments[0].file == "a.py"
    assert comments[0].line is None


class _RelocLLM:
    """LLM 重定位：返回 fenced code block 作为替换片段。"""

    def __init__(self, block: str, fail: bool = False) -> None:
        self._block = block
        self._fail = fail

    async def chat(self, _messages, _tools) -> AgentTurn:  # noqa: ANN001
        if self._fail:
            raise RuntimeError("reloc boom")
        return AgentTurn(content=f"```python\n{self._block}\n```")

    async def summarize(self, _f, _c) -> str:
        return "s"


async def test_relocation_llm_retry_and_failure_restore(tmp_path):
    # existing_code 不在 diff，LLM 返回正确片段 → 重试命中填 line
    src = _fd(content="alpha = 1\n")
    f = _finding(existing_code="WRONG_ORIGINAL")  # 不在新内容
    diff_map = {"a.py": src}
    reloc = _RelocLLM("alpha = 1")
    comments = await resolve_leaked_lines([f], diff_map, reloc, AgentConfig())
    assert comments[0].line == 1

    # LLM 失败 → 还原原片段，保持无 line（不猜不抛）
    f2 = _finding(existing_code="WRONG_ORIGINAL")
    comments2 = await resolve_leaked_lines([f2], diff_map, _RelocLLM("ignored", fail=True),
                                           AgentConfig())
    assert comments2[0].line is None
    assert comments2[0].existing_code == "WRONG_ORIGINAL"


# ── review_filter ──────────────────────────────────────────────────────


def _filter_llm(tool: str, comment_ids: list | None = None):
    class _F:
        async def chat(self, _messages, _tools):  # noqa: ANN001
            if tool == "approve":
                return AgentTurn(tool_calls=[ToolCall("approve_all_comments", {})])
            return AgentTurn(tool_calls=[ToolCall("report_incorrect_comments",
                                                  {"comment_ids": comment_ids or []})])

        async def summarize(self, _f, _c):
            return "s"

    return _F()


async def test_review_filter_report_incorrect_deletes():
    findings = [_finding(content="c0"), _finding(content="c1", file="b.py")]
    kept = await run_review_filter(_filter_llm("report", ["c-0"]), findings,
                                   group_diff_text="+ x = 1\n")
    assert [k.content for k in kept] == ["c1"]


async def test_review_filter_approve_keeps_all_and_bad_ids_ignored():
    findings = [_finding(content="c0"), _finding(content="c1")]
    kept = await run_review_filter(_filter_llm("approve"), findings, group_diff_text="")
    assert len(kept) == 2
    # 越界 id + 无调用（空回合）→ 全留（宁可保留）
    kept2 = await run_review_filter(_filter_llm("report", ["c-99", "bogus"]), findings,
                                    group_diff_text="")
    assert len(kept2) == 2


def test_parse_filter_result():
    cid = {"comment_ids": ["c-0", "c-2"]}
    turn = AgentTurn(tool_calls=[ToolCall("report_incorrect_comments", cid)])
    assert parse_filter_result(turn, 3) == {0, 2}
    approve = AgentTurn(tool_calls=[ToolCall("approve_all_comments", {})])
    assert parse_filter_result(approve, 5) == set()
    assert parse_filter_result(AgentTurn(), 5) == set()


# ── scoring ────────────────────────────────────────────────────────────


def test_scoring_parses_json_and_clamps():
    scores = _parse_scores(
        '{"correctness": 35, "security": 99, "practices": 20,'
        ' "performance": 4, "commit_quality": 4}')
    assert (scores.correctness, scores.security, scores.practices) == (35, 30, 20)
    assert scores.total == 35 + 30 + 20 + 4 + 4


async def test_scoring_failure_returns_zero():
    class _ScoringLLM:
        async def chat(self, _m, _t):  # noqa: ANN001
            return AgentTurn(content="not json")

        async def summarize(self, _f, _c):
            return "s"

    scores = await run_scoring(_ScoringLLM(), [{"role": "u", "content": ""}])
    assert scores.total == 0  # 解析失败归 0，不抛异常


# ── 集成：全阶段机 ─────────────────────────────────────────────────────


class _PhaseLLM:
    """按工具集路由阶段：main(code_comment/task_done) → filter(approve_all) → scoring(JSON)。"""

    def __init__(self) -> None:
        self.phase_names: list[str] = []

    async def chat(self, messages, tools):  # noqa: ANN001
        names = [t["function"]["name"] for t in tools] if tools else []
        if "code_comment" in names:
            self.phase_names.append("main")
            return AgentTurn(tool_calls=[
                ToolCall("code_comment", {"comments": [{
                    "path": "a.py", "content": "潜在 bug", "category": "bug",
                    "severity": "high", "existing_code": "x = 1",
                }]}),
                ToolCall("task_done", {"state": "DONE"}),
            ])
        if names == ["report_incorrect_comments", "approve_all_comments"]:
            self.phase_names.append("filter")
            return AgentTurn(tool_calls=[ToolCall("approve_all_comments", {})])
        if not tools:
            self.phase_names.append("scoring_or_plan")
            return AgentTurn(content=json.dumps(
                {"correctness": 36, "security": 27, "practices": 18,
                 "performance": 4, "commit_quality": 4}))
        return AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])

    async def summarize(self, _f, _c):
        return "s"


async def test_integration_full_phase_machine(tmp_path):
    # 小变更（不触发 plan）；main 报带 existing_code 的无行意见 → re_location 确定性填 line；
    # filter 全批过 → 保留；末尾 scoring 填 0-100。
    diffs = [_fd(content="x = 1\n")]
    # 物化文件供 re_location 匹配（FakeRuntime 按 new_file_content 写入）
    (tmp_path / "a.py").write_text("x = 1\n", "utf-8")
    runtime = FakeRuntime()
    llm_box = {}
    llm = _PhaseLLM()

    def factory():
        llm_box["llm"] = llm
        return llm

    result = await run_agentic_review(runtime, factory, diffs)
    assert len(result.findings) == 1
    assert result.findings[0].line == 1  # re_location 确定性填行
    assert result.findings[0].side == "RIGHT"
    assert "main" in llm.phase_names
    assert "filter" in llm.phase_names
    assert result.scores.total == 36 + 27 + 18 + 4 + 4  # scoring 收尾填 0-100
    assert result.scores.correctness == 36


# 小工具：内联 asyncio 跑协程（方便在同步测试里驱动 resolve_leaked_lines）


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)

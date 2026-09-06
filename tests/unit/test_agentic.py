"""离线单测（M5.6）：agentic 六工具 + llmloop 压缩/空轮/grace + 预算闸门 + 沙箱编排。

全部用 fake/in-process 驱动，不触真实 LLM/Docker：
- tools：临时物化目录跑只读工具——`..` 路径穿越拒绝、read_file ≤500 行、缺 path 回退 groupKey。
- llmloop：fake AgentLLM 走工具往返→code_comment 进 comments；空轮超限收束；
  grace round 只放 code_comment/task_done；同步压缩落地。
- budget：超预算 stop 不再调度剩余组。
- sandbox：FakeRuntime 物化只读工作区→code_comment 进 result(source=agent)；LLM 异常
  或沙箱关 → 降级普通 diff 审查仍产出。
"""

from __future__ import annotations

import json

import pytest

from codereview_ai.domain.models import (
    Category,
    ChangeType,
    FileDiff,
    Finding,
    PullRequest,
    ReviewResult,
    Severity,
)
from codereview_ai.review.agentic.budget import BudgetGate, estimate_tokens
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentTurn, ToolCall, run_agent_session
from codereview_ai.review.agentic.sandbox import (
    FakeRuntime,
    SandboxDisabled,
    run_agentic_review,
)
from codereview_ai.review.agentic.tools import (
    MAX_READ_LINES,
    RepoContext,
    ToolRunner,
    ToolState,
    tool_schemas,
)

# ── fake LLM：按消息内容翻牌子出回合 ───────────────────────────────────


class _FakeLLM:
    """顺序消费 `_plays` 出回合；summarize 返回罐头摘要。"""

    def __init__(self, plays: list[AgentTurn], summary: str = "已压缩的中段摘要") -> None:
        self._plays = list(plays)
        self._summary = summary
        self.failed_summarize = False

    async def chat(self, _messages, _tools) -> AgentTurn:
        return self._plays.pop(0) if self._plays else AgentTurn()

    async def summarize(self, _frozen, _compress) -> str:
        if self.failed_summarize:
            raise RuntimeError("summarize boom")
        return self._summary


def _new_ctx(tmp_path) -> RepoContext:
    return RepoContext(workspace=tmp_path, group_key="fallback.py")


def _comment_tuple(path="a.py") -> dict:
    return {"path": path, "content": "这里要修", "category": "bug", "severity": "high"}


# ── 六工具（§12.1）────────────────────────────────────────────────────


def test_read_file_lines_and_binary(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"l{i}" for i in range(600)), "utf-8")
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02")
    ctx = _new_ctx(tmp_path)
    runner = ToolRunner(ctx, ToolState())
    out = runner.run_one("read_file", {"file_path": "a.py"})
    payload = json.loads(out)
    assert payload["truncated"] is True
    assert len(payload["lines"]) <= MAX_READ_LINES

    out_bin = runner.run_one("read_file", {"file_path": "blob.dat"})
    assert "二进制" in out_bin


def test_grep_repo_and_file_find(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "backend.py").write_text("import os\nos.system('x')\n", "utf-8")
    ctx = _new_ctx(tmp_path)
    runner = ToolRunner(ctx, ToolState())
    grep = runner.run_one("grep_repo", {"search_text": "os.system"})
    assert "backend.py:2" in grep
    find = runner.run_one("file_find", {"query_name": "backend"})
    assert "sub/backend.py" in find


def test_path_traversal_rejected(tmp_path):
    ctx = RepoContext(workspace=tmp_path, group_key="")
    assert ctx.resolve("../secret") is None
    assert ctx.resolve("/etc/passwd") is None
    assert ctx.resolve("a.py") is not None


def test_code_comment_fallback_group_key(tmp_path):
    ctx = RepoContext(workspace=tmp_path, group_key="fallback.py")
    state = ToolState()
    runner = ToolRunner(ctx, state)
    runner.run_one("code_comment", {"comments": [_comment_tuple(path="")]})
    assert len(state.comments) == 1
    f = state.comments[0]
    assert f.file == "fallback.py"
    assert f.category == Category.BUG
    assert f.severity == Severity.HIGH
    assert f.source == "agent"


def test_code_comment_normalizes_bad_category(tmp_path):
    ctx = _new_ctx(tmp_path)
    state = ToolState()
    runner = ToolRunner(ctx, state)
    runner.run_one("code_comment", {"comments": [
        {"path": "b.py", "content": "x", "category": "nope", "severity": "urgent"}]})
    f = state.comments[0]
    assert f.category == Category.OTHER
    assert f.severity == Severity.LOW


def test_task_done_flags(tmp_path):
    ctx = _new_ctx(tmp_path)
    state = ToolState()
    runner = ToolRunner(ctx, state)
    assert runner.run_one("task_done", {"state": "DONE"}) == "DONE"
    assert state.done and not state.failed


def test_tool_schemas_grace_only_two(tmp_path):
    names = tool_schemas(["code_comment", "task_done"])
    assert {t["function"]["name"] for t in names} == {"code_comment", "task_done"}


# ── llmloop（§12.3）───────────────────────────────────────────────────


async def test_llmloop_tool_roundtrip(tmp_path):
    # read_file → code_comment → task_done
    (tmp_path / "a.py").write_text("x = 1\n", "utf-8")
    ctx = _new_ctx(tmp_path)
    llm = _FakeLLM([
        AgentTurn(tool_calls=[ToolCall("read_file", {"file_path": "a.py"})]),
        AgentTurn(tool_calls=[ToolCall("code_comment", {"comments": [_comment_tuple()]})]),
        AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
    ])
    result = await run_agent_session(llm, ToolRunner(ctx, ToolState()), "审查 a.py")
    assert result.done
    assert result.reason == "task_done"
    assert len(result.comments) == 1
    assert result.comments[0].source == "agent"


async def test_llmloop_empty_rounds_exhaust(tmp_path):
    ctx = _new_ctx(tmp_path)
    llm = _FakeLLM([AgentTurn()] * 5)
    result = await run_agent_session(
        llm, ToolRunner(ctx, ToolState()), "审查",
        cfg=AgentConfig(max_empty_turns=2),
    )
    assert result.reason == "empty_rounds_exceeded"
    assert not result.done


async def test_llmloop_grace_run_only_two_tools(tmp_path):
    ctx = _new_ctx(tmp_path)
    seen_tools: list[list[str]] = []

    class _GraceLLM:
        async def chat(self, _m, tools) -> AgentTurn:
            seen_tools.append([t["function"]["name"] for t in tools])
            return AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])

        async def summarize(self, _f, _c) -> str:
            return "s"

    result = await run_agent_session(
        _GraceLLM(), ToolRunner(ctx, ToolState()), "审查",
        cfg=AgentConfig(max_prompt_tokens=5),  # 极小预算→基础 system+intro 即触顶
    )
    assert result.reason in {"task_done", "grace_round"}
    assert seen_tools and set(seen_tools[-1]) == {"code_comment", "task_done"}


async def test_llmloop_sync_compress_applies_summary(tmp_path):
    ctx = _new_ctx(tmp_path)
    # 制造超 80% 的对话量，触发同步压缩
    big = "语" * 80_000  # ~80k 字符 → 远超 80% 阈值
    llm = _FakeLLM([
        AgentTurn(content="开头叙述" + big),
        AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
    ])
    result = await run_agent_session(
        llm, ToolRunner(ctx, ToolState()), "审查",
        cfg=AgentConfig(max_prompt_tokens=50_000),
    )
    assert result.reason == "task_done"


async def test_llmloop_compress_failure_keeps_messages(tmp_path):
    ctx = _new_ctx(tmp_path)
    big = "语" * 80_000
    llm = _FakeLLM([
        AgentTurn(content="开头" + big),
        AgentTurn(content="收尾"),
        AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
    ])
    llm.failed_summarize = True
    result = await run_agent_session(
        llm, ToolRunner(ctx, ToolState()), "审查",
        cfg=AgentConfig(max_prompt_tokens=50_000),
    )
    # 压缩失败不截断：仍能继续完成会话，不抛异常
    assert result.reason == "task_done"


# ── 预算闸门（§12.4）──────────────────────────────────────────────────


def test_budget_within_budget_schedules_all():
    gate = BudgetGate(max_budget=100)
    assert gate.can_schedule(30)
    gate.schedule(30)
    assert gate.can_schedule(60)
    gate.schedule(60)
    assert not gate.stopped


def test_budget_over_stops_remaining():
    gate = BudgetGate(max_budget=100)
    gate.schedule(80)
    assert not gate.can_schedule(30)  # 80+30>100，停
    gate.stop([30, 40])
    assert gate.stopped
    assert gate.skipped_groups == 2


def test_estimate_tokens_counts_diffs():
    diffs = [FileDiff(  # 400 字符→~100 token
        "a.py", "a.py", "+x*400", 400, 0, ChangeType.MODIFIED, "x" * 400
    )]
    assert 90 <= estimate_tokens(diffs) <= 110


# ── 沙箱编排（M5.6-2 / DESIGN §12.2）──────────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="gitlab", repo_id="1", repo_full_name="o/r", web_url="",
        pr_number=1, title="t", source_branch="s", target_branch="m",
        head_sha="a" * 12, base_sha="b" * 12,
    )


def _diffs(tmp_path, path="a.py", content="x = 1\n") -> list[FileDiff]:
    return [FileDiff(path, path, f"+ {content}", 1, 0, ChangeType.MODIFIED, content)]


async def test_run_agentic_review_source_agent(tmp_path):
    _diffs(tmp_path)  # 建真实待物化文件
    runtime = FakeRuntime()

    def factory():
        return _FakeLLM([
            AgentTurn(tool_calls=[ToolCall("read_file", {"file_path": "a.py"})]),
            AgentTurn(tool_calls=[ToolCall("code_comment", {"comments": [_comment_tuple()]})]),
            AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
        ])

    result = await run_agentic_review(runtime, factory, _diffs(tmp_path))
    assert len(result.findings) == 1
    assert result.findings[0].source == "agent"
    assert result.findings[0].file == "a.py"
    assert runtime._tmp is None  # stop 已清理


async def test_run_agentic_review_llm_failure_raises(tmp_path):
    runtime = FakeRuntime()

    class _Boom:
        async def chat(self, _m, _t) -> None:
            raise RuntimeError("llm down")

        async def summarize(self, _f, _c) -> str:
            return "s"

    with pytest.raises(RuntimeError):
        await run_agentic_review(runtime, lambda: _Boom(), _diffs(tmp_path))


class _FakeReviewer:
    async def review(self, *, pr, commits_text, diffs, static_findings_text=""):
        return ReviewResult(findings=[
            Finding(content="diff 意见", category=Category.BUG, severity=Severity.HIGH,
                    existing_code="", file="a.py"),
        ])


class _BrokenRuntime:
    async def guard(self) -> None:
        return None

    async def start(self, diffs) -> None:
        raise SandboxDisabled("沙箱默认关")

    async def stop(self) -> None:
        return None


async def test_strategy_agentic_degrades_to_diff(tmp_path):
    # strategy=agentic 但沙箱关 → 降级普通 diff 审查仍产出（§12.4 B9）
    from codereview_ai.worker import _review_agent_or_diff

    result = await _review_agent_or_diff(
        _FakeReviewer(), None, _pr(), "t", _diffs(tmp_path), [],
        strategy="agentic", agent_runtime=_BrokenRuntime(),
        agent_llm_factory=lambda: _FakeLLM([]),
    )
    assert len(result.findings) == 1
    assert result.findings[0].content == "diff 意见"


async def test_strategy_diff_uses_diff_review(tmp_path):
    from codereview_ai.worker import _review_agent_or_diff

    result = await _review_agent_or_diff(
        _FakeReviewer(), None, _pr(), "t", _diffs(tmp_path), [],
        strategy="diff", agent_runtime=None, agent_llm_factory=None,
    )
    assert result.findings[0].content == "diff 意见"

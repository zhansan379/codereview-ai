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

import asyncio
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


# ── 不可变 git 对象读（repo_dir + pinned_sha，免疫并发工作树 reset）──────────


_HAVE_GIT = __import__("shutil").which("git") is not None


def _seed_git(tmp_path):
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    for c in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *c], cwd=src, check=True, capture_output=True)
    (src / "a.txt").write_text("hi\n", "utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "backend.py").write_text("import os\nos.system('x')\n", "utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, check=True, capture_output=True, text=True
    ).stdout.strip()
    return src, head


@pytest.mark.skipif(not _HAVE_GIT, reason="本地无 git")
def test_git_context_reads_immutable(tmp_path):
    """repo_dir+pinned_sha 时三只读工具走 git 对象，工作树被覆盖也不影响读取。"""
    src, head = _seed_git(tmp_path)
    ctx = RepoContext(workspace=src, diff_map={}, repo_dir=src, pinned_sha=head)
    assert ctx.repo_git()
    runner = ToolRunner(ctx, ToolState())
    # 读、搜、找均基于 git 对象
    assert "hi" in runner.run_one("read_file", {"file_path": "a.txt"})
    assert "backend.py:2" in runner.run_one("grep_repo", {"search_text": "os.system"})
    assert "sub/backend.py" in runner.run_one("file_find", {"query_name": "backend"})
    # 核心断言：模拟并发 PR 把工作树覆盖成别的代码，读取仍返回 head sha 的原始内容
    (src / "a.txt").write_text("CLOBBERED\n", "utf-8")
    (src / "sub" / "backend.py").write_text("pass\n", "utf-8")
    out = runner.run_one("read_file", {"file_path": "a.txt"})
    assert "hi" in out and "CLOBBERED" not in out


def _make_bare(tmp_path, src):
    """把工作树 `src`（含头提交）克隆成 bare 仓库，返回 bare 目录。"""
    import subprocess

    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True, capture_output=True,
    )
    return bare


@pytest.mark.skipif(not _HAVE_GIT, reason="本地无 git")
def test_git_context_reads_from_bare_repo(tmp_path):
    """bare 仓库（无 working tree）下 repo_dir+pinned_sha 三只读工具仍按 git 对象读。"""
    import subprocess as sp

    src, head = _seed_git(tmp_path)
    bare = _make_bare(tmp_path, src)
    # bare 目录本身即 git 仓库根
    assert sp.run(["git", "rev-parse", "--is-bare-repository"], cwd=bare,
                  check=True, capture_output=True, text=True).stdout.strip() == "true"
    ctx = RepoContext(workspace=bare, diff_map={}, repo_dir=bare, pinned_sha=head)
    assert ctx.repo_git()
    runner = ToolRunner(ctx, ToolState())
    assert "hi" in runner.run_one("read_file", {"file_path": "a.txt"})
    assert "backend.py:2" in runner.run_one("grep_repo", {"search_text": "os.system"})
    assert "sub/backend.py" in runner.run_one("file_find", {"query_name": "backend"})
    # cat_file/blob 路径在 bare 下不可见文件实体，但对象可读（working tree 物化非必需）
    assert not (bare / "a.txt").exists()


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


def test_code_comment_line_anchor_parsed(tmp_path):
    # code_comment 带 line → Finding.line 填充（agent 读 clone 全仓的行号 = diff 新侧行号）
    ctx = _new_ctx(tmp_path)
    state = ToolState()
    runner = ToolRunner(ctx, state)
    runner.run_one("code_comment", {"comments": [
        {"path": "b.py", "content": "x", "line": 42, "old_line": 3}]})
    f = state.comments[0]
    assert f.line == 42
    assert f.old_line == 3
    # 非法/缺省 line → None（回落总结），不抛
    st2 = ToolState()
    ToolRunner(ctx, st2).run_one("code_comment", {"comments": [
        {"path": "b.py", "content": "y", "line": "abc"},
        {"path": "b.py", "content": "z"}]})
    assert all(f.line is None for f in st2.comments)


def test_task_done_flags(tmp_path):
    ctx = _new_ctx(tmp_path)
    state = ToolState()
    runner = ToolRunner(ctx, state)
    assert runner.run_one("task_done", {"state": "DONE"}) == "DONE"
    assert state.done and not state.failed


def test_tool_schemas_grace_only_two(tmp_path):
    names = tool_schemas(["code_comment", "task_done"])
    assert {t["function"]["name"] for t in names} == {"code_comment", "task_done"}


def test_code_comment_schema_is_nested_array_of_objects():
    # 修法回归：code_comment 的 comments 必须是标准 `array → items(object)` 嵌套结构——
    # 带类型、必填、category/severity 枚举。之前是 type:"string"，模型拿不到结构只能
    # 蒙，把正文塞进 content 外的键导致 _t_code_comment 整条丢弃（0 findings 根因）。
    schemas = {t["function"]["name"]: t["function"] for t in tool_schemas()}
    cc = schemas["code_comment"]
    comments = cc["parameters"]["properties"]["comments"]
    assert comments["type"] == "array"
    items = comments["items"]
    assert items["type"] == "object"
    props = items["properties"]
    assert props["path"]["type"] == "string"
    assert props["content"]["type"] == "string"
    assert props["line"]["type"] == "integer"
    assert set(items["required"]) == {"path", "content"}
    assert "bug" in props["category"]["enum"]
    assert props["severity"]["enum"] == ["critical", "high", "medium", "low"]


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


async def test_llmloop_tool_pair_wire_format(tmp_path):
    # OpenAI/DeepSeek 契约：assistant 带 tool_calls(含 id)，随后 tool 消息以 tool_call_id 指回。
    # 此 bug 离线 fake 看不见，只有真实 API 会拒（missing field tool_call_id）。
    (tmp_path / "a.py").write_text("x = 1\n", "utf-8")
    ctx = _new_ctx(tmp_path)
    seen: list[list[dict]] = []

    def make_llm():
        class _WireLLM:
            async def chat(self, messages, _tools):  # noqa: ANN001
                seen.append(messages)
                if not seen[0] or len([m for m in messages if m["role"] == "tool"]) == 0:
                    return AgentTurn(tool_calls=[ToolCall("read_file", {"file_path": "a.py"})])
                return AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])

            async def summarize(self, _f, _c) -> str:
                return "s"

        return _WireLLM()

    await run_agent_session(make_llm(), ToolRunner(ctx, ToolState()), "审查 a.py")
    # 第一次 chat 后，下一次会话里应出现完整配对：assistant(tool_calls) → tool(tool_call_id)
    pair_msgs = seen[1]
    assistant = next(m for m in pair_msgs if m["role"] == "assistant" and m.get("tool_calls"))
    cid = assistant["tool_calls"][0]["id"]
    tool = next(m for m in pair_msgs if m["role"] == "tool")
    assert cid and tool["tool_call_id"] == cid
    assert tool["name"] == "read_file"
    assert "tool_name" not in tool  # 非标准字段应去掉
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"


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


async def test_llmloop_iteration_cap_forces_conclusion(tmp_path):
    # 爱"只探索不收尾"的模型（只 read_file，永不 task_done）——迭代触顶必须进 grace，
    # 末轮只放 code_comment/task_done 逼它收敛；否则会撞 max_iterations 空手 break。
    ctx = _new_ctx(tmp_path)
    seen: list[set[str]] = []

    class _StallLLM:
        async def chat(self, _m, tools) -> AgentTurn:  # noqa: ANN001
            names = {t["function"]["name"] for t in tools}
            seen.append(names)
            if names == {"code_comment", "task_done"}:
                return AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])
            return AgentTurn(tool_calls=[ToolCall("read_file", {"file_path": "a.py"})])

        async def summarize(self, _f, _c) -> str:
            return "s"

    result = await run_agent_session(
        _StallLLM(), ToolRunner(ctx, ToolState()), "审查",
        cfg=AgentConfig(max_iterations=3),
    )
    assert result.reason == "task_done"
    assert seen and set(seen[-1]) == {"code_comment", "task_done"}  # 末轮工具被收窄逼收尾


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


async def test_run_agentic_review_line_goes_inline(tmp_path):
    # agent 报 line（=clone 全仓读到的新侧行号）→ finding.line 填充 → partition 分发到行级 inline
    raw = "--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n ctx\n+added\n"
    diffs = [FileDiff("a.py", "a.py", raw, 1, 0, ChangeType.MODIFIED, "ctx\nadded\n")]
    runtime = FakeRuntime()

    def factory():
        return _FakeLLM([
            AgentTurn(tool_calls=[ToolCall("code_comment", {"comments": [
                {"path": "a.py", "content": "问题", "category": "bug",
                 "severity": "high", "line": 2}]})]),
            AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
        ])

    result = await run_agentic_review(runtime, factory, diffs)
    assert len(result.findings) == 1
    assert result.findings[0].line == 2
    from codereview_ai.review.result_writer import partition_findings

    inline, textual = partition_findings(result.findings, diffs)
    assert len(inline) == 1  # 新增行 2 在可评论集合内 → 逐行 inline，而非总结
    assert textual == []


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

    async def start(self, pr, diffs) -> None:
        raise SandboxDisabled("沙箱默认关")

    async def stop(self) -> None:
        return None


async def test_strategy_agentic_degrades_to_diff(tmp_path):
    # strategy=agentic 但沙箱关 → 降级普通 diff 审查仍产出（§12.4 B9）
    from codereview_ai.worker import _review_agent_or_diff

    result, mode = await _review_agent_or_diff(
        _FakeReviewer(), None, _pr(), "t", _diffs(tmp_path), [],
        strategy="agentic", agent_runtime=_BrokenRuntime(),
        agent_llm_factory=lambda: _FakeLLM([]),
    )
    assert mode == "diff"  # 沙箱关 → 声明 agentic、实落 diff
    assert len(result.findings) == 1
    assert result.findings[0].content == "diff 意见"


async def test_strategy_agentic_zero_findings_falls_back_to_diff(tmp_path):
    # agentic 跑完但 0 条意见（agent 未调用 code_comment）→ 降级普通 diff 审查兜底，绝不高成空成功
    from codereview_ai.worker import _review_agent_or_diff

    result, mode = await _review_agent_or_diff(
        _FakeReviewer(), None, _pr(), "t", _diffs(tmp_path), [],
        strategy="agentic", agent_runtime=FakeRuntime(),
        agent_llm_factory=lambda: _FakeLLM(
            [AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])]),
    )
    assert mode == "diff"  # agent 非平凡 diff 产出 0 条 → 降级 diff
    assert len(result.findings) == 1
    assert result.findings[0].content == "diff 意见"  # 来自 diff 兜底，而非空 agentic
    assert result.findings[0].source != "agent"


async def test_strategy_diff_uses_diff_review(tmp_path):
    from codereview_ai.worker import _review_agent_or_diff

    result, mode = await _review_agent_or_diff(
        _FakeReviewer(), None, _pr(), "t", _diffs(tmp_path), [],
        strategy="diff", agent_runtime=None, agent_llm_factory=None,
    )
    assert mode == "diff"
    assert result.findings[0].content == "diff 意见"


# ── 组并发（§12.5：group_concurrency 有界信号量）─────────────────────────


class _ConcurrencyTracker:
    """跨组共享：记录 chat 里同时在飞的最大会话数，证明组是并发而非串行。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0


class _TrackedLLM:
    def __init__(self, tracker: _ConcurrencyTracker) -> None:
        self._tracker = tracker

    async def chat(self, _messages, _tools) -> AgentTurn:  # noqa: ANN001
        self._tracker.active += 1
        self._tracker.max_active = max(self._tracker.max_active, self._tracker.active)
        await asyncio.sleep(0.05)  # 人为制造重叠窗口，暴露并发度
        self._tracker.active -= 1
        return AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})])

    async def summarize(self, _frozen, _compress) -> str:
        return "s"


def _tracker_factory(tracker: _ConcurrencyTracker):
    return lambda: _TrackedLLM(tracker)


class _FakeGrouper:
    """每个文件单独成组，方便造出 N 组并发。"""

    async def group(self, diffs):  # noqa: ANN001
        return [[d] for d in diffs]


async def _run_concurrent(tmp_path, concurrency: int) -> _ConcurrencyTracker:
    diffs = [FileDiff(f"{p}.py", f"{p}.py", "+ x = 1\n", 1, 0, ChangeType.MODIFIED,
                      "x = 1\n") for p in ("a", "b", "c", "d")]
    for p in ("a", "b", "c", "d"):
        (tmp_path / f"{p}.py").write_text("x = 1\n", "utf-8")
    runtime = FakeRuntime()
    tracker = _ConcurrencyTracker()
    result = await run_agentic_review(
        runtime, _tracker_factory(tracker), diffs, cfg=AgentConfig(group_concurrency=concurrency),
        grouper=_FakeGrouper(),
    )
    assert result.summary.startswith("agentic 共报告")
    return tracker


async def test_group_concurrency_bounded_by_gather(tmp_path):
    # concurrency=4、4 组 → 峰值在飞 4（asyncio.gather + Semaphore 全放行）
    tracker = await _run_concurrent(tmp_path, 4)
    assert tracker.max_active == 4


async def test_group_concurrency_serial_when_one(tmp_path):
    # concurrency=1 → 串行，峰值在飞 1（信号量扣死）
    tracker = await _run_concurrent(tmp_path, 1)
    assert tracker.max_active == 1

# ── llm_adapter.summarize：OCR memory_compression 五维契约 ─────────────
def test_summarize_uses_memory_compression_template():
    """压缩用 OCR 五维契约（保留 file+severity）取代原中文一句话，上下文仍是压缩对象。"""
    import asyncio

    from codereview_ai.review.agentic.llm_adapter import ToolCallingLLM
    from codereview_ai.review.agentic.prompts import MEMORY_COMPRESSION_SYSTEM

    seen: dict = {}

    class _M:
        content = "五维摘要"

    class _C:
        message = _M()

    class _R:
        choices = [_C()]

    async def fake_backend(messages, tools):
        seen["messages"] = messages
        return _R()

    llm = ToolCallingLLM(model="fake/model", backend=fake_backend)
    asyncio.run(llm.summarize("sys", [{"role": "user", "content": "待压缩上下文"}]))

    msgs = seen["messages"]
    # user 位装的是 OCR memory_compression 契约模板（保留文件路径 + 严重度的五维结构）
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == MEMORY_COMPRESSION_SYSTEM
    assert "Confirmed Code Issues" in msgs[1]["content"] or "Identified Code Issues" in msgs[1]["content"]
    # 原中文一句话已移除
    assert "把下面的对话压缩成一段简短中文摘要" not in msgs[1]["content"]
    # compress_messages 仍作为 {{context}} 原样追加在后
    assert msgs[-1] == {"role": "user", "content": "待压缩上下文"}

def test_agent_loop_roundtrips_deepseek_reasoning_content(tmp_path):
    # DeepSeek thinking 模式：assistant 回合必须把 reasoning_content **原样带回**重申，
    # 否则循环重构 assistant 消息丢思考链 → 下一轮 chat 被 API 拒（BadRequestError）。
    # 断言第 2 轮 chat 收到的历史里，第 1 轮 assistant 条目带 reasoning_content。
    ctx = _new_ctx(tmp_path)

    seen: list[list[dict]] = []

    class _ThinkingLLM:
        def __init__(self) -> None:
            self._played = False

        async def chat(self, messages, tools):
            seen.append(list(messages))
            if not self._played:
                self._played = True
                return AgentTurn(
                    content="",
                    tool_calls=[ToolCall(name="grep_repo", args={"search_text": "foo"})],
                    reasoning_content="思考：先确认 foo 在哪",
                )
            return AgentTurn(
                content="审毕",
                tool_calls=[ToolCall(name="task_done", args={})],
                reasoning_content="思考：确认无遗漏",
            )

        async def summarize(self, _f, _c) -> str:
            return "sum"

    result = asyncio.run(run_agent_session(
        _ThinkingLLM(), ToolRunner(ctx, ToolState()), "审查 a.py",
    ))
    assert result.reason == "task_done"
    assert len(seen) == 2  # 恰两次 chat
    assistant = next(m for m in seen[1] if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == "思考：先确认 foo 在哪"


def test_agent_loop_omits_reasoning_content_for_plain_model(tmp_path):
    # 非 thinking 模型不产 reasoning_content → assistant 条目不应带该 key（避免 API 误读）。
    ctx = _new_ctx(tmp_path)
    seen: list[list[dict]] = []

    class _PlainLLM:
        def __init__(self) -> None:
            self._played = False

        async def chat(self, messages, tools):
            seen.append(list(messages))
            if not self._played:
                self._played = True
                return AgentTurn(content="", tool_calls=[ToolCall(name="grep_repo", args={"search_text": "x"})])
            return AgentTurn(content="审毕", tool_calls=[ToolCall(name="task_done", args={})])

        async def summarize(self, _f, _c) -> str:
            return "sum"

    asyncio.run(run_agent_session(_PlainLLM(), ToolRunner(ctx, ToolState()), "审查 a.py"))
    assistant = next(m for m in seen[1] if m.get("role") == "assistant")
    assert "reasoning_content" not in assistant
    assert assistant["content"] is None  # 带工具调用的 assistant content 为 null（OpenAI/DeepSeek 契约）

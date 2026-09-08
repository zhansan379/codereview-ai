"""Agentic 接线单测（Task 5）：syncer clone / LLM adapter / 内容指纹去重。

覆盖本批补齐的三个环节：
- syncer：临时起一个裸 git 远端做 tiny_remote，验证懒 clone + 幂等二次同步。
- llm_adapter：fake backend 驱动 `ToolCallingLLM.chat` 解析 tool_calls、`summarize`
  取正文、失败归一 `LLMError`。
- 去重护栏：`finding_matches` 的 body/code 指纹语义 + `run_agentic_review` 跨组去重。

说明：syncer 测试依赖本机 git，`RepoCloner.available()` 为假时跳过（不阻断 CI）。
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from codereview_ai.domain.models import (
    Category,
    ChangeType,
    FileDiff,
    Finding,
    Severity,
)
from codereview_ai.review.agentic.llm_adapter import ToolCallingLLM
from codereview_ai.review.agentic.llmloop import AgentTurn, ToolCall
from codereview_ai.review.agentic.sandbox import FakeRuntime, run_agentic_review
from codereview_ai.review.agentic.syncer import RepoCloner
from codereview_ai.review.agentic.tools import finding_matches
from codereview_ai.review.llm_gateway import LLMError

# ── syncer（tiny_remote）───────────────────────────────────────────────


def _git(cmd: list[str], cwd=None):
    subprocess.run(["git", *cmd], cwd=cwd, check=True, capture_output=True, text=True)


def _seed_remote(tmp_path):
    """建一个含一个提交的远端仓库，返回 (remote_as_uri, head_sha)。"""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init"], cwd=src)
    _git(["config", "user.email", "t@t"], cwd=src)
    _git(["config", "user.name", "t"], cwd=src)
    (src / "a.txt").write_text("hi\n", "utf-8")
    _git(["add", "."], cwd=src)
    _git(["commit", "-m", "init"], cwd=src)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(src), str(remote)])
    return remote.as_uri(), head


def test_slugify_key_sanitizes():
    from codereview_ai.review.agentic.syncer import slugify_key

    assert slugify_key("owner/repo") == "owner_repo"
    assert slugify_key("a.b-c_d") == "a.b-c_d"


def test_git_clone_url_from_web_url():
    from codereview_ai.domain.models import PullRequest
    from codereview_ai.review.agentic.syncer import git_clone_url

    pr = PullRequest(
        provider="github", repo_id="1", repo_full_name="o/r",
        web_url="https://github.com/o/r/pull/12", pr_number=12, title="t",
        source_branch="s", target_branch="m", head_sha="a" * 12, base_sha="b" * 12,
    )
    assert git_clone_url(pr) == "https://github.com/o/r.git"
    assert git_clone_url(PullRequest(  # 无 web_url → 返回 ""
        provider="github", repo_id="1", repo_full_name="o/r", web_url="",
        pr_number=1, title="t", source_branch="s", target_branch="m",
        head_sha="a" * 12, base_sha="b" * 12,
    )) == ""


@pytest.mark.skipif(not RepoCloner.available(), reason="本地无 git")
def test_cloner_sync_to_clone_and_idempotent(tmp_path):
    uri, head = _seed_remote(tmp_path)
    cloner = RepoCloner(tmp_path / "cache")
    ws = cloner.sync_to(url=uri, key="owner/repo", ref=head, token="")
    assert (ws / "a.txt").read_text("utf-8") == "hi\n"
    # 幂等二次同步：同一仓库、同一 sha 仍可用且路径一致
    ws2 = cloner.sync_to(url=uri, key="owner/repo", ref=head, token="")
    assert ws2 == ws
    assert (ws / "a.txt").exists()


@pytest.mark.skipif(not RepoCloner.available(), reason="本地无 git")
def test_cloner_sync_to_missing_url_raises(tmp_path):
    cloner = RepoCloner(tmp_path / "cache")
    with pytest.raises(RuntimeError):
        cloner.sync_to(url="", key="k", ref="abc")


@pytest.mark.skipif(not RepoCloner.available(), reason="本地无 git")
def test_cloner_recovers_corrupt_repo(tmp_path):
    """残缺 .git（被中断的 clone，只剩 hooks/info 模板）会被识别无效并重新 clone。"""
    uri, head = _seed_remote(tmp_path)
    cloner = RepoCloner(tmp_path / "cache")
    cache_dir = tmp_path / "cache" / "owner_repo"  # slugify_key("owner/repo")=owner_repo
    # 模拟半途被中断的 clone：.git 下仅有 git init 最早创建的 hooks/info
    (cache_dir / ".git" / "hooks").mkdir(parents=True)
    (cache_dir / ".git" / "info").mkdir(parents=True)
    ws = cloner.sync_to(url=uri, key="owner/repo", ref=head, token="")
    assert (ws / "a.txt").read_text("utf-8") == "hi\n"
    # 残缺目录被清掉重 clone，得到完整 .git/HEAD
    assert (cache_dir / ".git" / "HEAD").exists()


# ── llm_adapter（fake backend）────────────────────────────────────────


def _msg(tool_calls):
    return SimpleNamespace(
        content="c",
        tool_calls=tool_calls or None,
    )


def _tool_call(name: str, args: dict, cid: str = "c_1"):
    return SimpleNamespace(
        id=cid,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


async def test_adapter_chat_parses_tool_calls():
    async def backend(messages, tools):
        return SimpleNamespace(choices=[SimpleNamespace(message=_msg([
            _tool_call("code_comment", {"path": "a.py"}),
            _tool_call("grep_repo", {"search_text": "x", "case_sensitive": True}),
        ]))])

    llm = ToolCallingLLM(model="m", backend=backend)
    turn = await llm.chat([], [])
    assert turn.content == "c"
    assert [t.name for t in turn.tool_calls] == ["code_comment", "grep_repo"]
    # 真实 LLM 返回的 call.id 透传到 ToolCall，供会话循环按 OpenAI 契约配对 tool 消息
    assert turn.tool_calls[0].id == "c_1"
    assert turn.tool_calls[0].raw_arguments == json.dumps({"path": "a.py"})
    assert turn.tool_calls[1].args == {"search_text": "x", "case_sensitive": True}


async def test_adapter_chat_bad_arguments_tolerated():
    # 坏 JSON 参数不中断整轮，args 归一为空 dict
    async def backend(messages, tools):
        return SimpleNamespace(choices=[SimpleNamespace(message=_msg([
            _tool_call("code_comment", "{not json}"),
        ]))])

    llm = ToolCallingLLM(model="m", backend=backend)
    turn = await llm.chat([], [])
    assert turn.tool_calls[0].args == {}


async def test_adapter_summarize_extracts_content():
    async def backend(messages, tools):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="摘要正文", tool_calls=None))])

    llm = ToolCallingLLM(model="m", backend=backend)
    s = await llm.summarize("sys", [])
    assert s == "摘要正文"


async def test_adapter_backend_failure_normalizes_llm_error():
    async def boom(messages, tools):
        raise RuntimeError("nope")

    llm = ToolCallingLLM(model="m", backend=boom)
    with pytest.raises(LLMError):
        await llm.chat([], [])
    with pytest.raises(LLMError):
        await llm.summarize("sys", [])


# ── 内容指纹去重护栏 ───────────────────────────────────────────────────


def _finding(content: str, file: str = "a.py", code: str = "") -> Finding:
    return Finding(content=content, category=Category.BUG, severity=Severity.HIGH,
                   existing_code=code, file=file)


def test_finding_matches_body_fingerprint_ignores_ws_case():
    a = _finding("  Here  is\n A Bug ")
    b = _finding("here is a bug")
    assert finding_matches(a, b)
    assert finding_matches(b, a)


def test_finding_matches_code_fingerprint_or_body():
    a = _finding("不相干正文", code="x = 1")
    d = _finding("另一段不相干正文", code="x = 1")  # 仅 code 命中
    assert finding_matches(a, d)


def test_finding_matches_different_content_and_code_reject():
    a = _finding("foo", code="x = 1")
    c = _finding("bar", code="y = 2")
    assert not finding_matches(a, c)


def test_finding_matches_different_file_reject():
    a = _finding("same", file="a.py")
    e = _finding("same", file="b.py")
    assert not finding_matches(a, e)


# ── 跨组去重（run_agentic_review）─────────────────────────────────────


class _PlayLLM:
    """顺序消费 plays 出回合的 fake LLM（与 test_agentic 内同类最小实现）。"""

    def __init__(self, plays: list[AgentTurn]) -> None:
        self._plays = list(plays)

    async def chat(self, _m, _t) -> AgentTurn:
        return self._plays.pop(0) if self._plays else AgentTurn()

    async def summarize(self, _f, _c) -> str:
        return "s"


class _FakeGrouper:
    """把 diffs 对半切开成两组（≥GROUPING_MIN_FILES 时 `_group` 才调用 grouper）。"""

    async def group(self, diffs):
        mid = len(diffs) // 2
        return [diffs[:mid], diffs[mid:]]


def _many_diffs(n=4) -> list[FileDiff]:
    return [
        FileDiff(f"f{i}.py", f"f{i}.py", "+x", 1, 0, ChangeType.MODIFIED, "x")
        for i in range(n)
    ]


def _dup_comment() -> dict:
    return {"path": "f0.py", "content": "重复意见", "category": "bug", "severity": "high"}


async def test_run_agentic_review_dedups_across_groups():
    # 两组 agent 对同一文件上报同样意见 → 指纹去重，只留一条
    runtime = FakeRuntime()

    def factory():
        return _PlayLLM([
            AgentTurn(tool_calls=[ToolCall("code_comment", {"comments": [_dup_comment()]})]),
            AgentTurn(tool_calls=[ToolCall("task_done", {"state": "DONE"})]),
        ])

    result = await run_agentic_review(runtime, factory, _many_diffs(), grouper=_FakeGrouper())
    assert len(result.findings) == 1
    assert result.findings[0].content == "重复意见"
    assert result.findings[0].source == "agent"

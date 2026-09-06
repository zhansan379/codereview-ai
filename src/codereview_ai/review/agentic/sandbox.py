"""Agentic 沙箱编排（DESIGN §12.2 / M5.6-2）：只读沙箱运行时 + 每文件组独立 agent 循环。

- `SandboxRuntime` Protocol；离线 `FakeRuntime` 进程内物化只读工作区并驱动六工具环；
  真实 `DockerRuntime` 构造 `docker run --network=none -u nobody --cpus 1 --memory 512m
  --pids-limit 64` 只读挂载，**默认关**（`enabled=False` 时 start 抛 `SandboxDisabled`），
  本轮不真正执行容器。
- `run_agentic_review`：把 diff 分组，每组一个独立 `AgentLLM`（独立 trace_id）跑
  `run_agent_session`，`code_comment` 收集为 `Finding(source="agent")`；任意阶段异常
  **整体抛出**，由主链降级为普通 diff 审查（保证至少一条 review 落回，DESIGN §12.4 B9）。

离线可测：FakeRuntime + fake LLM 驱动六工具 → source=agent；异常 → 降级仍产出。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from codereview_ai.domain.models import FileDiff, ReviewResult
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentLLM, run_agent_session
from codereview_ai.review.agentic.tools import RepoContext, ToolRunner, ToolState
from codereview_ai.review.group_review import GROUPING_MIN_FILES
from codereview_ai.review.grouping import SemanticGrouper


class SandboxDisabled(RuntimeError):
    """沙箱未启用（默认关）或运行时不支持（Docker 未装）。"""


class SandboxRuntime(Protocol):
    """只读沙箱：物化工作区（在真实实现为容器只读挂载），停用时清理。"""

    async def start(self, diffs: list[FileDiff]) -> RepoContext: ...
    async def stop(self) -> None: ...
    async def guard(self) -> None:
        """跑前检查沙箱可用（容器未启用/daemon 未起 → 抛 SandboxDisabled）。"""
        ...


def _materialize(diffs: list[FileDiff], root: Path) -> dict[str, str]:
    """按 `new_file_content` 物化新侧文件到工作区，返回 path->diff 文本映射（§12.2）。"""
    diff_map: dict[str, str] = {}
    for d in diffs:
        key = d.new_path.removeprefix("/")
        diff_map[key] = d.diff
        if d.new_file_content and not (root / key).exists():
            target = root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(d.new_file_content, "utf-8")
    return diff_map


class FakeRuntime:
    """进程内只读运行时：临时目录物化工作区，停用删除（离线可测，§12.2）。"""

    def __init__(self) -> None:
        self._tmp: Path | None = None
        self._diff_map: dict[str, str] = {}

    async def guard(self) -> None:
        return  # 进程内假运行时常可 用

    async def start(self, diffs: list[FileDiff]) -> RepoContext:
        self._tmp = Path(tempfile.mkdtemp(prefix="cr-agent-ws-"))
        self._diff_map = _materialize(diffs, self._tmp)
        return RepoContext(workspace=self._tmp, diff_map=self._diff_map)

    async def stop(self) -> None:
        if self._tmp is not None and self._tmp.exists():
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None


class DockerRuntime:
    """真实容器只读沙箱（DESIGN §12.2）：默认关，本轮只构造命令不执行。

    `docker run --rm --network=none -v <repo>:<repo>:ro -v <ws> -u nobody
    --cpus 1 --memory 512m --pids-limit 64 <image> <cmd>`；结束 `docker rm -f`。
    `enabled=False`（默认）时 start 抛 `SandboxDisabled`，主链据此降级。
    """

    def __init__(self, image: str = "codereview-ai/agent:local", enabled: bool = False) -> None:
        self.image = image
        self.enabled = enabled
        self.command: str = ""
        self.diff_map: dict[str, str] = {}

    async def guard(self) -> None:
        if not self.enabled:
            raise SandboxDisabled("Docker 沙箱默认关（M5.6 延后，等 docker daemon 可用）")

    async def start(self, diffs: list[FileDiff]) -> RepoContext:
        await self.guard()
        ws = Path(tempfile.mkdtemp(prefix="cr-agent-docker-"))
        _materialize(diffs, ws)
        # 只构造命令，本轮不真正执行（daemon 未运行，DESIGN §19 延后真容器冒烟）
        self.command = (
            f"docker run --rm --network=none "
            f"-v {ws.as_posix()}:/repo:ro -v {ws.as_posix()}/ws "
            f"--user nobody --cpus 1 --memory 512m --pids-limit 64 {self.image}"
        )
        self.diff_map = {d.new_path.removeprefix("/"): d.diff for d in diffs}
        return RepoContext(workspace=ws, diff_map=self.diff_map)

    async def stop(self) -> None:
        self.command = ""  # docker rm -f 由真实编排在容器结束后执行；本轮无容器可删


async def _group(diffs: list[FileDiff], grouper: SemanticGrouper | None) -> list[list[FileDiff]]:
    """分组：大变更按语义分组，否则整组一次（与 review_in_groups 同规约，§7.5）。"""
    if grouper is None or len(diffs) < GROUPING_MIN_FILES:
        return [list(diffs)] if diffs else []
    try:
        groups = await grouper.group(diffs)
    except Exception as exc:  # noqa: BLE001 —— 分组失败退整组，不阻断沙箱审查
        raise SandboxDisabled(f"agentic 分组失败：{exc}") from exc
    return groups or [list(diffs)]


async def run_agentic_review(
    runtime: SandboxRuntime,
    llm_factory: Callable[[], AgentLLM],
    diffs: list[FileDiff],
    grouper: SemanticGrouper | None = None,
    cfg: AgentConfig | None = None,
) -> ReviewResult:
    """按文件组各跑独立 agent 会话，收集 `code_comment` 为 `Finding(source="agent")`。

    每组一个全新 `AgentLLM`（独立 trace_id，组间不共享记忆）；若干组共享同一只读
    工作区但各持独立会话。任一步骤异常（含 agent 标记 FAILED）**整体抛出**→ 主链
    降级为普通 diff 审查（DESIGN §12.4 B9，保证至少一条 review 落回）。运行时无论
    成功失败都在 finally 清理物化资源。
    """
    ctx = await runtime.start(diffs)
    try:
        groups = await _group(diffs, grouper)
        seen: set[tuple[str, str]] = set()
        result = ReviewResult()
        for g in groups:
            llm = llm_factory()  # 每组独立 LLM/独立 trace
            group_key = g[0].new_path if g else ""
            runner = ToolRunner(
                RepoContext(workspace=ctx.workspace, diff_map=ctx.diff_map, group_key=group_key),
                ToolState(),
            )
            paths = ",".join(sorted({d.new_path for d in g}))
            turn = await run_agent_session(llm, runner, f"审查文件组：{paths}", cfg=cfg)
            if turn.failed:
                raise SandboxDisabled("agent 标记 FAILED，降级为普通 diff 审查")
            for f in turn.comments:
                fp = (f.file, f.content)
                if fp in seen:
                    continue
                seen.add(fp)
                result.findings.append(f)
        result.summary = f"agentic 共报告 {len(result.findings)} 条意见"
        return result
    finally:
        await runtime.stop()

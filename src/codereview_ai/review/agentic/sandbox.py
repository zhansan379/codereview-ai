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

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from codereview_ai.domain.models import FileDiff, Finding, PullRequest, ReviewResult
from codereview_ai.review.agentic.capture import set_phase
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentLLM, run_agent_session
from codereview_ai.review.agentic.planner import (
    build_plan_messages,
    plan_required,
    run_plan_phase,
)
from codereview_ai.review.agentic.prompt_builder import build_main_task_intro, render_diffs
from codereview_ai.review.agentic.relocation import resolve_leaked_lines
from codereview_ai.review.agentic.review_filter import run_review_filter
from codereview_ai.review.agentic.scoring import build_score_messages, run_scoring
from codereview_ai.review.agentic.syncer import (
    RepoCloner,
    TokenProvider,
    git_clone_url,
    slugify_key,
)
from codereview_ai.review.agentic.tools import (
    RepoContext,
    ToolRunner,
    ToolState,
    finding_matches,
)
from codereview_ai.review.group_review import GROUPING_MIN_FILES
from codereview_ai.review.grouping import SemanticGrouper


class SandboxDisabled(RuntimeError):
    """沙箱未启用（默认关）或运行时不支持（Docker 未装）。"""


class SandboxRuntime(Protocol):
    """只读沙箱：物化工作区（在真实实现为容器只读挂载），停用时清理。"""

    async def start(self, pr: PullRequest, diffs: list[FileDiff]) -> RepoContext: ...
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

    async def start(self, pr: PullRequest, diffs: list[FileDiff]) -> RepoContext:
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

    async def start(self, pr: PullRequest, diffs: list[FileDiff]) -> RepoContext:
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


class LocalCloneRuntime:
    """进程内只读工作区：把被审仓库 clone 到本地做全仓上下文（生产默认运行时）。

    借鉴旧项目 repo_syncer 的 clone 机制，但**不提供 shell**——agent 只能经只读结构化
    工具看这个 clone 的工作树，无任意代码执行，故不需要 docker 容器隔离。cache 目录跨
    审查复用：`RepoCloner` 增量 fetch 到位，不重复 clone。停用不删 cache（保留复用）。

    `token_for` 是按 provider 取平台凭据的异步回调；缺省/无 token 时 clone 走公开地址
    （私有仓库会失败，由上层降级）。
    """

    def __init__(
        self,
        cache_root: Path | str,
        *,
        enabled: bool = True,
        token_for: TokenProvider | None = None,
    ) -> None:
        self._cloner = RepoCloner(cache_root)
        self._enabled = enabled
        self._token_for = token_for
        self._workspace: Path | None = None

    async def guard(self) -> None:
        if not self._enabled:
            raise SandboxDisabled("agentic 未启用（agent_review_enabled=False）")
        if not self._cloner.available():
            raise SandboxDisabled("本地无 git 可执行，无法 clone 全仓")

    async def start(self, pr: PullRequest, diffs: list[FileDiff]) -> RepoContext:
        await self.guard()
        url = git_clone_url(pr)
        token = ""
        if url and self._token_for is not None:
            token = await self._token_for(pr.provider) or ""
        workspace = await asyncio.to_thread(
            self._cloner.sync_to, url=url, key=slugify_key(pr.repo_full_name),
            ref=pr.head_sha, token=token,
        )
        self._workspace = workspace
        diff_map = _materialize(diffs, workspace)  # 兜底：确保变更文件也存在
        # repo_dir+pinned_sha：使三个读文件工具走不可变 git 对象（按 head_sha 寻址），
        # 免疫并发审查下工作树被其它 PR reset 覆盖的竞态。
        return RepoContext(
            workspace=workspace, diff_map=diff_map,
            repo_dir=workspace, pinned_sha=pr.head_sha,
        )

    async def stop(self) -> None:
        self._workspace = None  # 保留 cache 供下次复用，不删除


def _fingerprint_dup(f: Finding, seen: list[Finding]) -> bool:
    """判断是否与已收集评论重复（内容指纹命中，见 tools.finding_matches 语义）。"""
    return any(finding_matches(seen_f, f) for seen_f in seen)


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
    pr: PullRequest | None = None,
    grouper: SemanticGrouper | None = None,
    cfg: AgentConfig | None = None,
) -> ReviewResult:
    """按文件组各跑独立 agent 会话，收集 `code_comment` 为 `Finding(source="agent")`。

    每组一个全新 `AgentLLM`（独立 trace_id，组间不共享记忆）；若干组共享同一只读
    工作区但各持独立会话。`pr` 供 clone 型运行时（`LocalCloneRuntime`/`DockerRuntime`）
    取 clone 地址与 head_sha；进程内 FakeRuntime 忽略之。任一步骤异常（含 agent 标记
    FAILED）**整体抛出**→ 主链降级为普通 diff 审查（DESIGN §12.4 B9，保证至少一条
    review 落回）。运行时无论成功失败都在 finally 清理物化资源。
    """
    ctx = await runtime.start(pr, diffs)
    cfg = cfg or AgentConfig()
    try:
        groups = await _group(diffs, grouper)
        concurrency = max(1, cfg.group_concurrency)
        sem = asyncio.Semaphore(concurrency)
        # 全部 diff 的 path→FileDiff（含 new_file_content），供 re_location 跨文件唯一串搜
        #（只读共享，并发安全；`_norm_key` 语义 = `new_path.removeprefix("/")`）。
        file_diffs = {d.new_path.removeprefix("/"): d for d in diffs}

        # 每组独立走 OCR 阶段机：Plan → Main → re_location → review_filter。独立 LLM/trace、
        # 独立 ToolRunner/ToolState，可安全并发（只读 clone 工作区共享，仅去重寄存器是下文
        # merge 的共享可变状态，加锁守 check-append 原子性）。
        async def _run_one(g: list[FileDiff]) -> list[Finding]:
            async with sem:
                llm = llm_factory()
                group_key = g[0].new_path if g else ""
                runner = ToolRunner(
                    RepoContext(workspace=ctx.workspace, diff_map=ctx.diff_map,
                                group_key=group_key),
                    ToolState(),
                )
                change_files = sorted({d.new_path for d in g})
                diff_text = render_diffs(g)

                # A. Plan（OCR PlanRequired 门控才跑，失败非致命）
                set_phase("plan")
                plan = ""
                if cfg.plan_enabled and plan_required(
                    g,
                    line_threshold=cfg.plan_line_threshold,
                    group_line_threshold=cfg.plan_group_line_threshold,
                ):
                    plan = await run_plan_phase(
                        llm, build_plan_messages(diffs=g, change_files=change_files))

                # B. Main loop（注入 plan/checklist，逐文件不跳过）
                set_phase("main")
                intro = build_main_task_intro(diffs=g, change_files=change_files, plan=plan)
                turn = await run_agent_session(llm, runner, intro, cfg=cfg)
                if turn.failed:
                    raise SandboxDisabled("agent 标记 FAILED，降级为普通 diff 审查")

                # C. re_location 钉行阶梯（无行且带 existing_code 的意见重锚）
                set_phase("re_location")
                comments = await resolve_leaked_lines(turn.comments, file_diffs, llm, cfg)

                # D. review_filter 事实核查（只删 diff 铁证反驳的错评，存疑批过）
                if cfg.review_filter_enabled:
                    set_phase("review_filter")
                    comments = await run_review_filter(
                        llm, comments, group_diff_text=diff_text)

                return comments

        group_results = await asyncio.gather(*(_run_one(g) for g in groups))

        # 内容指纹去重（见 tools.finding_fingerprint）：同文件且 body/code 指纹命中即跳过，
        # 防同问题在多组/多轮/多会话重复上报（PR-Agent body_fp OR code_fp 护栏）。串行时
        # 无竞争；并发后去重寄存器是唯一共享可变状态，合并紧凑加锁守 check-append 原子性。
        result = ReviewResult()
        merge_lock = asyncio.Lock()

        async def _merge(comments: list[Finding]) -> None:
            async with merge_lock:
                for f in comments:
                    if _fingerprint_dup(f, result.findings):
                        continue
                    result.findings.append(f)

        await asyncio.gather(*(_merge(c) for c in group_results))

        # E. 评分收尾（OCR 没有，我们自创救回 0-100 卡片）：对合并后全部 findings 打一次分
        if cfg.scoring_enabled and result.findings:
            set_phase("scoring")
            scoring_llm = llm_factory()
            result.scores = await run_scoring(
                scoring_llm,
                build_score_messages(findings=result.findings,
                                     group_diff_text=render_diffs(diffs)),
            )
        result.summary = f"agentic 共报告 {len(result.findings)} 条意见"
        return result
    finally:
        await runtime.stop()

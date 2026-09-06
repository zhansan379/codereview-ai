"""webhook → 队列 → worker 的封装（simple 档，DESIGN §6.3/§9/§15.3）。

- `EventStore`：task_id → (provider, raw_body) 的进程内暂存（队列只搬 task_id，
  payload 由 worker 取出后解析，DESIGN §9.1）。
- `QueueEnqueuer`：匹配 webhook 契约 `async enqueue(provider, raw)`，投队列并入 store。
- `process_raw_event`：原始 payload → 解析 PR（mr 轨）或 push 事件（push 轨 §7.7）→
  过滤 action → 审查 → 回写；有 `review_repo` 时把两轨的结果/审计行真落库。
- push 轨：默认关（`PushGate`），差量三分支取 diff，只产**一条**总结评论回写 head commit；
  幂等靠 `uq_review_push` 抢占（DESIGN §7.7）。
- 网络与 LLM 只出现在 forge/reviewer 注入里；测试可全程离线。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from fnmatch import fnmatch
from dataclasses import dataclass
from typing import Any

from codereview_ai.domain.models import FileDiff, Finding, PullRequest, PushEvent, ReviewResult
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.queue.base import TaskMeta, TaskQueue
from codereview_ai.review.agentic.llmloop import AgentLLM
from codereview_ai.review.agentic.sandbox import SandboxDisabled, SandboxRuntime, run_agentic_review
from codereview_ai.review.group_review import review_in_groups
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.review.increments import (
    REASON_ALREADY,
    IncrementReference,
    IncrementStore,
    collect_fingerprints,
    decide_from_ref,
    dedup_findings,
)
from codereview_ai.review.result_writer import ResultWriter
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.review.static_analysis import StaticAnalyzer
from codereview_ai.storage.project_repo import ProjectConfig
from codereview_ai.storage.review_repo import ReviewRepository

logger = logging.getLogger("codereview_ai.worker")

#: forge / reviewer 工厂：按 provider 给出对应的审查设施（测试注入 fake）。
#: forge 可能返回 None（该 provider 未配置适配器），worker 跳过而非报错。
ForgeFactory = Callable[[str], ForgeAdapter | None]
ReviewerFactory = Callable[[str], Reviewer]
#: 项目配置工厂：按 (provider, repo_id) 返回该项目启用行的审查配置（可为 None）。
ProjectConfigFactory = Callable[[str, str], Awaitable[ProjectConfig | None]]


def parse_file_extensions(raw: str) -> frozenset[str]:
    """把逗号分隔的扩展名串整形成小写无前导点集合。

    `".py, .ts\\n" → frozenset({"py", "ts"})`；空/空白 → 空集（表示不过滤）。
    """
    out: set[str] = set()
    for part in (raw or "").split(","):
        ext = part.strip().lstrip(".").lower()
        if ext:
            out.add(ext)
    return frozenset(out)


def apply_extension_filter(diffs: list[FileDiff], extensions: str) -> list[FileDiff]:
    """按扩展名过滤 diff 列表（DESIGN 文件扩展名过滤）。

    `extensions` 为空 → 原样返回（审全部，向后兼容）；否则只保留扩展名命中的文件
    （`new_path` 为主、`old_path` 兜底——删除文件的场景）。大小写不敏感。
    """
    exts = parse_file_extensions(extensions)
    if not exts:
        return diffs

    def _ext(d: FileDiff) -> str:
        path = d.new_path or d.old_path
        return os.path.splitext(path)[1].lstrip(".").lower()

    return [d for d in diffs if _ext(d) in exts]


async def _project_cfg(
    factory: ProjectConfigFactory | None, provider: str, repo_id: str
) -> ProjectConfig | None:
    """取项目配置；未注入 factory → None（不破坏无 DB 的调用/旧测试）。"""
    if factory is None:
        return None
    try:
        cfg = await factory(provider, repo_id)
    except Exception:  # noqa: BLE001 配置读取失败降级为不过滤，不影响审查主链
        logger.warning("读取项目配置失败（%s@%s），按不过滤处理", provider, repo_id)
        return None
    return cfg


def _compile_push_globs(globs: str) -> Callable[[str], bool] | None:
    """把逗号分隔 fnmatch 分支 glob 编译成 `branch -> bool`；空则 None（启用时全放行）。

    与 `main._branch_glob_match` 语义一致，供项目级 push 分支规则覆盖用（DESIGN §7.7）。
    """
    patterns = [p.strip() for p in globs.split(",") if p.strip()]
    if not patterns:
        return None

    def match(branch: str) -> bool:
        return any(fnmatch(branch, p) for p in patterns)

    return match


@dataclass
class PushGate:
    """push 轨审查的开关 + 分支过滤（DESIGN §7.7：默认关闭，避免刷屏）。

    `enabled=False` 时任何分支都不走 LLM（仍会落审计行并标 skipped）。
    `branch_match(branch)` 命中才审；None 表示启用时全分支放行。
    """

    enabled: bool = False
    branch_match: Callable[[str], bool] | None = None

    def should(self, branch: str) -> bool:
        if not self.enabled:
            return False
        return self.branch_match(branch) if self.branch_match else True


async def _review_agent_or_diff(
    reviewer: Reviewer,
    grouper: SemanticGrouper | None,
    pr: PullRequest,
    commits_text: str,
    diffs: list[FileDiff],
    static_findings: list[Finding] | None,
    strategy: str,
    agent_runtime: SandboxRuntime | None,
    agent_llm_factory: Callable[[], AgentLLM] | None,
) -> ReviewResult:
    """按 `strategy` 调度审查：`agentic` 走沙箱，否则普通 diff 分组审查。

    agentic 需 `agent_runtime` 与 `agent_llm_factory` 都配置；任一步骤异常（含沙箱
    默认关 `SandboxDisabled`）→ **整条降级为 diff 审查**（DESIGN §12.4 B9），保证
    至少一条普通 review 落回，不把 agent 的失败转成任务级 failed 丢失审查。
    """
    if strategy == "agentic" and agent_runtime is not None and agent_llm_factory is not None:
        try:
            return await run_agentic_review(
                agent_runtime, agent_llm_factory, diffs, grouper=grouper,
            )
        except SandboxDisabled as exc:
            logger.warning("agentic 不可用，降级为普通 diff 审查：%s", exc)
    return await review_in_groups(
        reviewer, grouper, pr=pr, commits_text=commits_text, diffs=diffs,
        static_findings=static_findings,
    )


def _commits_text(ev: PushEvent) -> str:
    """push 的提交历史文本（首个几分钟条），供 reviewer prompt 与回写展示用。"""
    if not ev.commits:
        return ev.after
    return "\n".join(c.message for c in ev.commits)


def _push_as_pr(ev: PushEvent) -> PullRequest:
    """push 事件套上最小 PullRequest 壳，让 reviewer/分组管线可复用（无 MR 序号）。"""
    return PullRequest(
        provider=ev.provider, repo_id=ev.repo_id, repo_full_name=ev.repo_full_name,
        web_url="", pr_number=0, title=_commits_text(ev), source_branch=ev.branch,
        target_branch=ev.branch, head_sha=ev.after, base_sha=ev.before,
    )


#: severity → 展示符号（push 总结 Markdown 用）。
_SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}


def build_push_summary(ev: PushEvent, result: ReviewResult) -> str:
    """push 轨总结评论：只产一条（无行级，§7.7）。"""
    scores = result.scores
    lines = [
        f"🤖 AI 代码审查 · {ev.repo_full_name}@{ev.branch}",
        f"最新提交：`{ev.after[:8]}`",
        "",
        f"**总分 {scores.total} / 100**",
        "| 维度 | 得分 |",
        "|---|---|",
        f"| 正确性 | {scores.correctness}/40 |",
        f"| 安全 | {scores.security}/30 |",
        f"| 工程实践 | {scores.practices}/20 |",
        f"| 性能 | {scores.performance}/5 |",
        f"| 提交质量 | {scores.commit_quality}/5 |",
        "",
        result.summary,
    ]
    if result.findings:
        lines += ["", "---", "**发现的问题：**", ""]
        for f in result.findings:
            icon = _SEVERITY_ICON.get(str(f.severity), "⚪")
            lines.append(f"- {icon} **[{str(f.category)}]** {f.file}：{f.content}")
    if result.skipped_files:
        skip = [f"- {p}" for p in result.skipped_files]
        lines += ["", "---", "**被过滤、未审查的文件：**", *skip]
    return "\n".join(lines)


def _is_all_zero(sha: str) -> bool:
    """after/before 全 0 判断：删分支/新分支的分支哨兵（§7.7）。"""
    return bool(sha) and set(sha) == {"0"}


async def _run_static(
    analyzer: StaticAnalyzer | None, diffs: list[FileDiff]
) -> list[Finding]:
    """跑静态分析（DESIGN §11）。未配置/失败都降级返回空列表，绝不阻断主链。"""
    if analyzer is None or not diffs:
        return []
    try:
        return await analyzer.analyze(diffs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("静态分析异常降级：%s", exc)
        return []


class EventStore:
    """task_id → (provider, raw_body) 的进程内暂存。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, bytes]] = {}

    def put(self, task_id: str, provider: str, raw: bytes) -> None:
        self._items[task_id] = (provider, raw)

    def get(self, task_id: str) -> tuple[str, bytes] | None:
        return self._items.get(task_id)

    def drop(self, task_id: str) -> None:
        self._items.pop(task_id, None)


class QueueEnqueuer:
    """webhook 契约 `async enqueue(provider, raw)` → 入队 + 暂存 payload。"""

    def __init__(self, queue: TaskQueue, store: EventStore) -> None:
        self._queue = queue
        self._store = store

    async def enqueue(self, provider: str, raw: bytes) -> str:
        meta = await self._queue.enqueue(provider)
        self._store.put(meta.task_id, provider, raw)
        return meta.task_id


def _event_action(data: dict[str, Any]) -> str:
    """归一事件 action：优先 GitLab 的 object_attributes.action，其次顶层 action。"""
    oa = data.get("object_attributes")
    if isinstance(oa, dict) and oa.get("action"):
        return str(oa["action"])
    return str(data.get("action") or "")


async def process_raw_event(
    forge: ForgeAdapter,
    reviewer: Reviewer,
    raw: bytes,
    *,
    increments: IncrementStore | None = None,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    chain_valid: Callable[[str, str], bool] | None = None,
    notifier: NotifierDispatcher | None = None,
    push_gate: PushGate | None = None,
    static_analyzer: StaticAnalyzer | None = None,
    review_strategy: str = "diff",
    agent_runtime: SandboxRuntime | None = None,
    agent_llm_factory: Callable[[], AgentLLM] | None = None,
    project_config_factory: ProjectConfigFactory | None = None,
) -> None:
    """原始 webhook payload → 审查 + 回写。各阶段失败在此抛出，由 worker 标 failed。

    事件解析分双轨：mr（`parse_merge_request`）、push（`parse_push_event`，DESIGN §7.7）。
    增量（DESIGN §7.3）落点可来自 `review_repo`（DB 持久，M4 起主用）或进程内
    `increments`（M3 内存档兼容）。`grouper` 给定且改动 ≥ 4 个文件时走语义分组并
    发审查（DESIGN §7.5，见 review.group_review）。`chain_valid(prior_sha, head_sha)`
    校验上次 head 是否仍在本 PR 链上（平台 compare），缺省 `None` → 保守回退全量。
    `notifier`（DESIGN F4）给定时，审查+回写成功后后台推送 IM 通知（失败不影响主链）。
    `push_gate` 给定时判定 push 轨是否走 LLM（默认关）；给定 `review_repo` 时两轨结果
    真落库（review_task/review_finding）。`project_config_factory`（可选）按项目取
    `file_extensions` 过滤 diff（DESIGN 文件扩展名过滤）；未给定 → 不过滤。
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return  # 非 JSON 忽略（签名已验，恶意/畸形 payload 不触发审查）
    if not isinstance(data, dict):
        return
    pr = forge.parse_merge_request(data)
    if pr is None:
        ev = forge.parse_push_event(data)
        if ev is None:
            return  # 非 merge_request 也非 push 事件：任务即完成，无需回写
        await _review_push_event(
            forge, reviewer, ev, review_repo=review_repo, grouper=grouper,
            notifier=notifier, push_gate=push_gate, static_analyzer=static_analyzer,
            review_strategy=review_strategy, agent_runtime=agent_runtime,
            agent_llm_factory=agent_llm_factory, project_config_factory=project_config_factory,
            raw_payload=raw.decode("utf-8", "replace"),
        )
        return
    if not forge.should_review(_event_action(data)):
        return  # close/merge 等动作不触发审查

    # 取上次成功审查落点：DB 仓储优先，其次内存 store；都没有 → 全量
    ref: IncrementReference | None = None
    if review_repo is not None:
        ref = await review_repo.last_ok_review(pr.provider, pr.repo_id, pr.pr_number)
    elif increments is not None:
        ref = increments.last(pr.provider, pr.pr_number)

    valid = chain_valid(ref.head_sha, pr.head_sha) if chain_valid and ref else False
    decision = decide_from_ref(ref, pr, chain_valid=valid)
    if decision.reason == REASON_ALREADY:
        return  # 同一 commit 重放：已审过，跳过
    incremental = decision.is_incremental

# 先落任务行（幂等，key 同 head）：fetch / LLM 失败也落 failed 可见、可重试，
    # 避免坏 LLM 输出偶发时任务静默消失（与 push 轨 ensure_task-前置 一致）。
    task_id: int | None = None
    if review_repo is not None:
        task_id = await review_repo.ensure_task(
            provider=pr.provider, repo_id=pr.repo_id, pr_number=pr.pr_number,
            event_type="mr", branch=pr.source_branch, head_sha=pr.head_sha,
            base_sha=pr.base_sha, payload=raw.decode("utf-8", "replace"),
        )

    try:
        refreshed = await forge.fetch_pull_request(pr)  # 补 diff_refs（行级评论 position 必填）
        diffs = await forge.fetch_files(refreshed)
        # 项目级文件扩展名过滤：只审命中的文件（DESIGN 文件扩展名过滤）
        cfg = await _project_cfg(project_config_factory, refreshed.provider, refreshed.repo_id)
        if cfg and cfg.file_extensions:
            diffs = apply_extension_filter(diffs, cfg.file_extensions)
        if not diffs:
            # 全部被扩展名滤掉：不调 LLM（省 token）；标 completed-empty
            if task_id is not None:
                await review_repo.mark_state(task_id, state="completed",
                                             summary_md="_扩展名过滤后无待审文件_",
                                             score_total=0)
            return
        # 静态分析先跑（DESIGN §11）：失败降级为空，不影响主链
        static_findings = await _run_static(static_analyzer, diffs)
        result = await _review_agent_or_diff(
            reviewer, grouper, pr=refreshed, commits_text=refreshed.title, diffs=diffs,
            static_findings=static_findings, strategy=review_strategy,
            agent_runtime=agent_runtime, agent_llm_factory=agent_llm_factory,
        )

        if incremental:
            # 只审增量：按内容指纹滤掉上次已报过的 finding（DESIGN §7.3 / §13.3）
            result.findings = dedup_findings(result.findings, ref)

        await ResultWriter(forge).write(refreshed, diffs, result)

        if review_repo is not None:
            # mr 轨真落库（幂等；同 head 已存在则跳过，不重复写）
            if task_id is not None:
                await review_repo.insert_findings(task_id, result.findings)
                await review_repo.mark_state(
                    task_id, state="completed", summary_md=result.summary,
                    score_total=result.scores.total,
                )

        if notifier is not None:
            # F4.4 fire-and-forget：推送后台化，不拖慢也不阻断审查主链
            notifier.launch(refreshed, result)

        if increments is not None and refreshed.head_sha:
            # 内存档才显式记录落点；DB 档 findigs 已落 review_finding，由 review_repo 读取
            increments.record(
                pr.provider, pr.pr_number, refreshed.head_sha, collect_fingerprints(result.findings)
            )
    except Exception as exc:
        # 失败落 failed 行（后台可见、可重试），再向上抛出由 worker 标队列 failed
        logger.warning("mr 轨审查失败（%s pr#%s）：%s", pr.repo_full_name, pr.pr_number, exc)
        if task_id is not None:
            try:
                await review_repo.mark_state(task_id, state="failed", error=str(exc)[:2000])
            except Exception:
                pass  # 落库失败不遮蔽原始异常
        raise


async def _review_push_event(
    forge: ForgeAdapter,
    reviewer: Reviewer,
    ev: PushEvent,
    *,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    notifier: NotifierDispatcher | None = None,
    push_gate: PushGate | None = None,
    static_analyzer: StaticAnalyzer | None = None,
    review_strategy: str = "diff",
    agent_runtime: SandboxRuntime | None = None,
    agent_llm_factory: Callable[[], AgentLLM] | None = None,
    project_config_factory: ProjectConfigFactory | None = None,
    raw_payload: str = "",
) -> None:
    """push 轨审查（DESIGN §7.7）：幂等落审计行 → 门控 → 差量三分支 → 单条总结回写。

    事件本身**始终**落一条审计行（幂等抢占，冲突即跳过）；仅当 push 开且分支规则命中
    才真正走 LLM。**不**做行级评论：push 没有 MR 可挂 inline，只回写一条总结到 head commit。
    """
    if not ev.before and not ev.after:
        return  # 构造缺失（无 before/after）→ 忽略，不审不落
    audit_id = 0  # 未配 DB（review_repo=None）时的占位，下面所有落库调用都被 `review_repo` 守卫
    force = False  # 手动重试意图：true 则绕过幂等预检 + push 门控强制执行（§7.7 补审）
    # 提前取一次项目配置：门控解析与文件扩展名过滤共用（项目开关改动实时生效）
    cfg = await _project_cfg(project_config_factory, ev.provider, ev.repo_id)
    if review_repo is not None:
        # push 幂等预检：同 (branch, after) 已有审计行 → 已处理过；除非该行为手动重试（force_rerun）
        existing = await review_repo.push_existing_audit(
            provider=ev.provider, repo_id=ev.repo_id, branch=ev.branch, head_sha=ev.after
        )
        force = bool(existing and existing.force_rerun)
        if existing is not None and not force:
            return  # 重复 webhook：该分支该 after 已审过/已跳过，跳过（§7.7）
        tid = await review_repo.ensure_task(
            provider=ev.provider, repo_id=ev.repo_id, pr_number=None, event_type="push",
            branch=ev.branch, head_sha=ev.after, base_sha=ev.before, payload=raw_payload,
        )
        if tid is None:
            return  # 并发下另一 worker 抢先插入 → 幂等跳过（§7.7）
        audit_id = tid
        if force:
            # 手动重试意图本次消费：清掉标记，本事件按强制重跑处理而非幂等跳过
            await review_repo.clear_force_rerun(audit_id)
    try:
        if _is_all_zero(ev.after):
            # 删分支：无 head 可审，不审也不回写（只留审计行，标 skipped，带原因）；补审对删分支无意义
            if review_repo is not None:
                await review_repo.mark_state(
                    audit_id, state="skipped", skip_reason="branch_deleted",
                    error="push 事件为删除分支，仅记录未审查",
                )
            return
        # 门控解析：enabled / branch_match = 全局 env 默认（push_gate）→ 项目显式覆盖（cfg）
        enabled = push_gate.enabled if push_gate is not None else False
        branch_match = push_gate.branch_match if push_gate is not None else None
        if cfg is not None and cfg.push_enabled is not None:
            enabled = cfg.push_enabled
        if cfg is not None and cfg.push_branch_globs:
            branch_match = _compile_push_globs(cfg.push_branch_globs)
        if force:
            # 手动补审：用户显式要审这条，无视门控直接走 LLM
            enabled, branch_match = True, None
        if not enabled:
            # 未开启：审计行标 skipped，带原因；门控/配置类跳过可由「补审」重试
            if review_repo is not None:
                await review_repo.mark_state(
                    audit_id, state="skipped", skip_reason="push_disabled",
                    error="push 审查未开启（默认关闭），仅记录未审查",
                )
            return
        if branch_match is not None and not branch_match(ev.branch):
            # 分支规则未命中：审计行标 skipped，带原因；改配置后可由「补审」重试
            if review_repo is not None:
                await review_repo.mark_state(
                    audit_id, state="skipped", skip_reason="branch_mismatch",
                    error="该分支未命中 push 审查规则，仅记录未审查",
                )
            return
        # 差量三分支：before 全 0 = 新分支 → 单 commit diff；其余 → compare（§7.7）
        if _is_all_zero(ev.before):
            diffs = await forge.get_first_commit_changes(ev)
        else:
            diffs = await forge.get_push_changes(ev)
        # 项目级文件扩展名过滤（DESIGN 文件扩展名过滤），push 轨同样生效（cfg 已在开头取）
        if cfg and cfg.file_extensions:
            diffs = apply_extension_filter(diffs, cfg.file_extensions)
        if not diffs:
            # 全部被扩展名滤掉：不调 LLM，审计行标 completed-empty
            if review_repo is not None:
                await review_repo.mark_state(audit_id, state="completed",
                                             summary_md="_扩展名过滤后无待审文件_", score_total=0)
            return
        pr = _push_as_pr(ev)
        static_findings = await _run_static(static_analyzer, diffs)
        result = await _review_agent_or_diff(
            reviewer, grouper, pr=pr, commits_text=_commits_text(ev), diffs=diffs,
            static_findings=static_findings, strategy=review_strategy,
            agent_runtime=agent_runtime, agent_llm_factory=agent_llm_factory,
        )
        summary = build_push_summary(ev, result)
        await forge.post_commit_summary(ev, summary)  # 只一条总结评论，无行级（§7.7）
        if notifier is not None:
            notifier.launch(pr, result)
        if review_repo is not None:
            await review_repo.insert_findings(audit_id, result.findings)
            await review_repo.mark_state(
                audit_id, state="completed", summary_md=result.summary,
                score_total=result.scores.total,
            )
    except Exception as exc:
        logger.warning("push 轨审查失败（%s@%s）：%s", ev.repo_full_name, ev.after, exc)
        if review_repo is not None:
            await review_repo.mark_state(audit_id, state="failed", error=str(exc)[:2000])
        raise


def make_processor(
    forge_factory: ForgeFactory,
    reviewer_factory: ReviewerFactory,
    store: EventStore,
    *,
    increments: IncrementStore | None = None,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    chain_valid: Callable[[str, str], bool] | None = None,
    notifier: NotifierDispatcher | None = None,
    push_gate: PushGate | None = None,
    static_analyzer: StaticAnalyzer | None = None,
    review_strategy: str = "diff",
    agent_runtime: SandboxRuntime | None = None,
    agent_llm_factory: Callable[[], AgentLLM] | None = None,
    project_config_factory: ProjectConfigFactory | None = None,
) -> Callable[[TaskMeta], Awaitable[None]]:
    """由 worker 主循环调用的处理函数：根据 task 取 payload 后走完整管线。

    `increments`/`review_repo`/`grouper`/`chain_valid`/`notifier`/`push_gate`/
    `static_analyzer`/`review_strategy`/`agent_runtime`/`agent_llm_factory`/
    `project_config_factory` 透传给 `process_raw_event`（都缺省时禁用增量/分组/推送/
    记录/静态分析/沙箱/扩展名过滤，见其 docstring）。
    """

    async def process(task: TaskMeta) -> None:
        item = store.get(task.task_id)
        if item is None:
            return  # 无暂存 payload（如直接入队的调试任务），视为已处理
        provider, raw = item
        forge = forge_factory(provider)
        reviewer = reviewer_factory(provider)
        if forge is None:
            logger.warning("provider %s 未配置适配器，任务 %s 跳过", provider, task.task_id)
            return
        await process_raw_event(
            forge,
            reviewer,
            raw,
            increments=increments,
            review_repo=review_repo,
            grouper=grouper,
            chain_valid=chain_valid,
            notifier=notifier,
            push_gate=push_gate,
            static_analyzer=static_analyzer,
            review_strategy=review_strategy,
            agent_runtime=agent_runtime,
            agent_llm_factory=agent_llm_factory,
            project_config_factory=project_config_factory,
        )

    return process

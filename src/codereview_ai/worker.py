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
from collections.abc import Awaitable, Callable
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
from codereview_ai.storage.review_repo import ReviewRepository

logger = logging.getLogger("codereview_ai.worker")

#: forge / reviewer 工厂：按 provider 给出对应的审查设施（测试注入 fake）。
#: forge 可能返回 None（该 provider 未配置适配器），worker 跳过而非报错。
ForgeFactory = Callable[[str], ForgeAdapter | None]
ReviewerFactory = Callable[[str], Reviewer]


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
) -> None:
    """原始 webhook payload → 审查 + 回写。各阶段失败在此抛出，由 worker 标 failed。

    事件解析分双轨：mr（`parse_merge_request`）、push（`parse_push_event`，DESIGN §7.7）。
    增量（DESIGN §7.3）落点可来自 `review_repo`（DB 持久，M4 起主用）或进程内
    `increments`（M3 内存档兼容）。`grouper` 给定且改动 ≥ 4 个文件时走语义分组并
    发审查（DESIGN §7.5，见 review.group_review）。`chain_valid(prior_sha, head_sha)`
    校验上次 head 是否仍在本 PR 链上（平台 compare），缺省 `None` → 保守回退全量。
    `notifier`（DESIGN F4）给定时，审查+回写成功后后台推送 IM 通知（失败不影响主链）。
    `push_gate` 给定时判定 push 轨是否走 LLM（默认关）；给定 `review_repo` 时两轨结果
    真落库（review_task/review_finding）。
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
            agent_llm_factory=agent_llm_factory,
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

    refreshed = await forge.fetch_pull_request(pr)  # 补 diff_refs（行级评论 position 必填）
    diffs = await forge.fetch_files(refreshed)
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
        task_id = await review_repo.ensure_task(
            provider=refreshed.provider, repo_id=refreshed.repo_id,
            pr_number=refreshed.pr_number, event_type="mr",
            branch=refreshed.source_branch, head_sha=refreshed.head_sha,
            base_sha=refreshed.base_sha,
        )
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
) -> None:
    """push 轨审查（DESIGN §7.7）：幂等落审计行 → 门控 → 差量三分支 → 单条总结回写。

    事件本身**始终**落一条审计行（幂等抢占，冲突即跳过）；仅当 push 开且分支规则命中
    才真正走 LLM。**不**做行级评论：push 没有 MR 可挂 inline，只回写一条总结到 head commit。
    """
    if not ev.before and not ev.after:
        return  # 构造缺失（无 before/after）→ 忽略，不审不落
    audit_id = 0  # 未配 DB（review_repo=None）时的占位，下面所有落库调用都被 `review_repo` 守卫
    if review_repo is not None:
        # push 幂等预检：同 (branch, after) 已有审计行 → 已审过/已跳过，直接跳过（§7.7）
        if await review_repo.push_already_audited(
            provider=ev.provider, repo_id=ev.repo_id, branch=ev.branch, head_sha=ev.after
        ):
            return
        tid = await review_repo.ensure_task(
            provider=ev.provider, repo_id=ev.repo_id, pr_number=None, event_type="push",
            branch=ev.branch, head_sha=ev.after, base_sha=ev.before,
        )
        if tid is None:
            return  # 并发下另一 worker 抢先插入 → 幂等跳过（§7.7）
        audit_id = tid
    try:
        if _is_all_zero(ev.after):
            # 删分支：不审也不回写（只留审计行，标 skipped）
            if review_repo is not None:
                await review_repo.mark_state(audit_id, state="skipped", error="delete_branch")
            return
        if push_gate is None or not push_gate.should(ev.branch):
            # 默认关或分支规则未命中：审计行标 skipped，不审
            if review_repo is not None:
                await review_repo.mark_state(audit_id, state="skipped")
            return
        # 差量三分支：before 全 0 = 新分支 → 单 commit diff；其余 → compare（§7.7）
        if _is_all_zero(ev.before):
            diffs = await forge.get_first_commit_changes(ev)
        else:
            diffs = await forge.get_push_changes(ev)
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
            await review_repo.mark_state(audit_id, state="failed", error=str(exc))
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
) -> Callable[[TaskMeta], Awaitable[None]]:
    """由 worker 主循环调用的处理函数：根据 task 取 payload 后走完整管线。

    `increments`/`review_repo`/`grouper`/`chain_valid`/`notifier`/`push_gate`/
    `static_analyzer`/`review_strategy`/`agent_runtime`/`agent_llm_factory` 透传给
    `process_raw_event`（都缺省时禁用增量/分组/推送/记录/静态分析/沙箱，见其 docstring）。
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
        )

    return process

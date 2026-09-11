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

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.domain.models import FileDiff, Finding, PullRequest, PushEvent, ReviewResult
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.logging import TRACE_ID
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.queue.base import TaskMeta, TaskQueue
from codereview_ai.review.agentic.capture import ConversationRecorder, conversation_capture, diff_usage_sink
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentLLM
from codereview_ai.review.agentic.sandbox import SandboxDisabled, SandboxRuntime, run_agentic_review
from codereview_ai.review.group_review import review_in_groups
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.storage.setting_repo import (
    MR_REVIEW_DEFAULT_KEY,
    PUSH_REVIEW_DEFAULT_KEY,
    SettingRepository,
)
from codereview_ai.review.increments import (
    REASON_ALREADY,
    IncrementReference,
    IncrementStore,
    collect_fingerprints,
    decide_from_ref,
    dedup_findings,
)
from codereview_ai.review.result_writer import ResultWriter, review_fingerprint
from codereview_ai.review.reuse import covered_file_map, prune_unchanged
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


def _count_diff_lines(diffs: list[FileDiff]) -> int:
    """diff 行数口径 = 新增+删除行合计（供 `review_task.diff_lines` 复杂度度量）。

    只数以 `+`/`-` 开头的变更行；跳过 `+++`/`---` 的 hunk 头文件标记（非实际改动）。
    """
    total = 0
    for d in diffs:
        for line in d.diff.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                total += 1
    return total


def _exec_metrics(recorder: ConversationRecorder | None) -> tuple[int, int]:
    """取 (chat_rounds, tool_calls)；未采集对话（recorder 为 None）记 0。"""
    if recorder is None:
        return 0, 0
    return recorder.metrics()


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
    agent_config: AgentConfig | None = None,
    usage_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[ReviewResult, str]:
    """按 `strategy` 调度审查：`agentic` 走沙箱，否则普通 diff 分组审查。

    返回 (result, exec_mode)：`exec_mode` 记录**实际走通**的路径（agentic 会在沙箱
    关闭 `SandboxDisabled`、或非平凡 diff 产出 0 条时降级为 diff）——调用方据此落库，
    让仪表盘能按真实模式区分 agent/diff。
    agentic 需 `agent_runtime` 与 `agent_llm_factory` 都配置；任一步骤异常（含沙箱
    默认关 `SandboxDisabled`）→ **整条降级为 diff 审查**（DESIGN §12.4 B9），保证
    至少一条普通 review 落回，不把 agent 的失败转成任务级 failed 丢失审查。
    """
    if strategy == "agentic" and agent_runtime is not None and agent_llm_factory is not None:
        try:
            result = await run_agentic_review(
                agent_runtime, agent_llm_factory, diffs, pr=pr, grouper=grouper,
                cfg=agent_config,
            )
            # 非平凡 diff 上 agentic 一条意见都没有，几乎不是「仓库很干净」，而是 agent
            # 没真正走到 code_comment（模型未按工具契约、或读完直接结束）。降级普通 diff
            # 审查兜底出版，绝不把空成功当结论（_looks_like_review 同款守卫）。
            if not result.findings:
                logger.warning(
                    "agentic 产出 0 条，降级普通 diff 审查兜底（diffs=%d）",
                    len(diffs),
                )
                return (
                    await review_in_groups(
                        reviewer, grouper, pr=pr, commits_text=commits_text, diffs=diffs,
                        static_findings=static_findings, usage_sink=usage_sink,
                    ),
                    "diff",
                )
            return result, "agentic"
        except SandboxDisabled as exc:
            logger.warning("agentic 不可用，降级为普通 diff 审查：%s", exc)
    diff_result = await review_in_groups(
        reviewer, grouper, pr=pr, commits_text=commits_text, diffs=diffs,
        static_findings=static_findings, usage_sink=usage_sink,
    )
    return diff_result, "diff"


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
    """task_id → (provider, payload) 的进程内暂存。

    payload 为 `bytes`（webhook 原始 body）或 `PullRequest`（补拉已解析的打开 PR）；worker
    消费时据此分流：前者走 `process_raw_event` 解析，后者直接 `review_pull_request`。
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, bytes | PullRequest]] = {}

    def put(self, task_id: str, provider: str, payload: bytes | PullRequest) -> None:
        self._items[task_id] = (provider, payload)

    def get(self, task_id: str) -> tuple[str, bytes | PullRequest] | None:
        return self._items.get(task_id)

    def drop(self, task_id: str) -> None:
        self._items.pop(task_id, None)


class QueueEnqueuer:
    """webhook 契约 `async enqueue(provider, raw)` → 入队 + 暂存 payload。

    `on_enqueue`（可空）：入队时异步执行的回调，用于「入队即建 mr 审计行」，让队列里
    等待的 PR 从入队起就在管理页可见为『排队中』；回调失败不阻断入队（审计由 process 兜底）。
    """

    def __init__(
        self,
        queue: TaskQueue,
        store: EventStore,
        *,
        on_enqueue: Callable[[str, bytes], Awaitable[None]] | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self.on_enqueue = on_enqueue

    async def enqueue(self, provider: str, raw: bytes) -> str:
        meta = await self._queue.enqueue(provider)
        self._store.put(meta.task_id, provider, raw)
        if self.on_enqueue is not None:
            try:
                await self.on_enqueue(provider, raw)
            except Exception as exc:  # noqa: BLE001 建行失败不阻断入队：审计/重试仍由 process 兜底
                logger.warning("入队建行失败（%s）：%s", provider, exc)
        return meta.task_id

    async def enqueue_pr(self, provider: str, pr: PullRequest) -> str:
        """补拉专用入队：携带一个**已解析**的 `PullRequest`（非原始 webhook body）。

        供 PRPoller 把打开的 PR 投给后台 worker 异步审查。**不触发** `on_enqueue`
        （`scribble_queued_task` 要 parse 原始 body 才建行，PR 没有）；调用方（补拉）
        已在入队前用 `ensure_task` 自己建好 queued 审计行，worker 消费时 `review_pull_request`
        的 `ensure_task` 命中该行 → 标 running → 异步审查。
        """
        meta = await self._queue.enqueue(provider)
        self._store.put(meta.task_id, provider, pr)
        return meta.task_id


async def replay_pending_tasks(
    review_repo: ReviewRepository, enqueuer: QueueEnqueuer
) -> int:
    """启动回放（DESIGN §9.2 / 崩溃恢复）：把遗留 queued/running 且带 payload 的任务
    重新投回内存队列，让上次被杀死的审查续跑。

    内存队列「重启即清空」(`queue.asyncio`)，DB 侧遗留的 queued 行不会被消费而变成
    孤死任务、无失败原因可查。此函数在 worker 启动前调用，扫描 DB 待回放行并据此
    重新入队。幂等安全：同 head 已 completed 的重放由增量决策/`ensure_task` 短路，
    不重复审查也不重复写 finding。
    """
    rows = await review_repo.pending_for_replay()
    for _task_id, provider, payload in rows:
        await enqueuer.enqueue(provider, payload.encode())
    return len(rows)


async def scribble_queued_task(
    review_repo: ReviewRepository, forge: ForgeAdapter | None, raw: bytes
) -> None:
    """入队即建 mr 审计行：让队列里等待的 PR 从入队起可见为『排队中』。

    单 worker 串行下，排在后头的任务原本只在开审时才由 `process_raw_event` 建行，排在
    长任务后面的会完全不可见。此函数在入队时用同一套 parse 幂等建行（state=queued）；
    worker 开审后同一个 `ensure_task` 命中该行 → 标 running（DESIGN §9.2）。

    安全：已审过的同 head 重放会被增量决策短路（REASON_ALREADY），重复 webhook 不建
    重复行也不重复审查。**只对 mr 轨建行**——push 轨靠 `push_existing_audit`「存在=已
    处理」做幂等预检，预建行会被误判成已处理而跳过；且 push 审计行本就是开审即建，
    排队不可见的窗口极小。
    """
    if forge is None:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    pr = forge.parse_merge_request(data)
    if pr is None or not forge.should_review(_event_action(data)):
        return  # 非 mr 或 close/merge 等不审动作：process 同样不建行，保持一致
    await review_repo.ensure_task(
        provider=pr.provider, repo_id=pr.repo_id, pr_number=pr.pr_number,
        event_type="mr", branch=pr.source_branch, head_sha=pr.head_sha,
        base_sha=pr.base_sha, pr_title=pr.title, pr_author=pr.author, web_url=pr.web_url,
        payload=raw.decode("utf-8", "replace"),
    )


def _event_action(data: dict[str, Any]) -> str:
    """归一事件 action：优先 GitLab 的 object_attributes.action，其次顶层 action。"""
    oa = data.get("object_attributes")
    if isinstance(oa, dict) and oa.get("action"):
        return str(oa["action"])
    return str(data.get("action") or "")


# ── 并发防重：按 (provider, repo_id, head_sha) 键控的进程内锁 ─────────────
# 手动补拉、定时补拉、webhook worker 皆同一进程/同一事件循环，因此用 per-head
# `asyncio.Lock` 即可关死 `ensure_task` 落 queued 到 mark running 之间的并发窗口
# （删记录后重拉同 head，第二生产者会在该窗口拿到同一 task id → 双评论/双通知）。
# 第二生产者等锁后再算增量决策，读到的已是 first 写好的 completed 锚点 → REASON_ALREADY
# → 返回 "already"，不会重复。
_head_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_head_locks_guard = asyncio.Lock()


async def _head_lock(key: tuple[str, str, str]) -> asyncio.Lock:
    async with _head_locks_guard:
        lock = _head_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _head_locks[key] = lock
        return lock


async def review_pull_request(
    forge: ForgeAdapter,
    reviewer: Reviewer,
    pr: PullRequest,
    **kwargs: Any,
) -> str:
    """mr 轨审查唯一入口（薄壳）：按 head 加锁，防并发重复审查，再委托 `_do_review_pull_request`。"""
    lock = await _head_lock((pr.provider, pr.repo_id, pr.head_sha))
    async with lock:
        return await _do_review_pull_request(forge, reviewer, pr, **kwargs)


async def _do_review_pull_request(
    forge: ForgeAdapter,
    reviewer: Reviewer,
    pr: PullRequest,
    *,
    increments: IncrementStore | None = None,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    chain_valid: Callable[[str, str], bool] | None = None,
    notifier: NotifierDispatcher | None = None,
    static_analyzer: StaticAnalyzer | None = None,
    review_strategy: str = "diff",
    agent_runtime: SandboxRuntime | None = None,
    agent_llm_factory: Callable[[], AgentLLM] | None = None,
    agent_config: AgentConfig | None = None,
    engine: AsyncEngine | None = None,
    agent_conversation_enabled: bool = True,
    agent_reuse_enabled: bool = True,
    project_config_factory: ProjectConfigFactory | None = None,
    raw_payload: str = "",
    mr_default_enabled: bool | None = None,
) -> str:
    """审查一条**已解析**的 PR（mr 轨核心，webhook 与主动补拉共用，DESIGN §7.3/§9）。

    入参是中立 `PullRequest`（webhook 由 `parse_merge_request` 产出；补拉由
    `forge.list_pulls` 产出），避免两条入口各写一份审查编排。**不重做**动作门控
    （调用方负责：webhook 先过 `should_review(action)`，补拉天然审打开 PR）。其余全部
    逻辑与 webhook 原 mr 轨一致：增量决策(`decide_from_ref`)→ 幂等 ensure_task →
    fetch→过滤→审查→对账落库→commit status→通知。`raw_payload` 仅在 webhook 路径真传
    原始 body（供 payload 重放/重试）；补拉路径为空字符串（新 head 再轮询即重审）。

    返回状态串供补拉统计：`"already"`（同 head 已审过，跳过）、`"empty"`（扩展名过滤
    后无待审文件，标 completed-empty）、`"reviewed"`（本次完成审查）。异常向上抛。
    """
    # 取上次成功审查落点：DB 仓储优先，其次内存 store；都没有 → 全量
    ref: IncrementReference | None = None
    if review_repo is not None:
        ref = await review_repo.last_ok_review(pr.provider, pr.repo_id, pr.pr_number)
    elif increments is not None:
        ref = increments.last(pr.provider, pr.pr_number)

    valid = chain_valid(ref.head_sha, pr.head_sha) if chain_valid and ref else False
    decision = decide_from_ref(ref, pr, chain_valid=valid)
    if decision.reason == REASON_ALREADY:
        return "already"  # 同一 commit 重放：已审过，跳过
    incremental = decision.is_incremental

    # 先落任务行（幂等，key 同 head）作为**认领**：fetch / LLM 失败也落 failed 可见、可重试，
    # 避免坏 LLM 输出偶发时任务静默消失（与 push 轨 ensure_task-前置 一致）。
    task_id: int | None = None
    if review_repo is not None:
        task_id = await review_repo.ensure_task(
            provider=pr.provider, repo_id=pr.repo_id, pr_number=pr.pr_number,
            event_type="mr", branch=pr.source_branch, head_sha=pr.head_sha,
            base_sha=pr.base_sha, pr_title=pr.title, pr_author=pr.author, web_url=pr.web_url,
            payload=raw_payload, trace_id=TRACE_ID.get(),
        )
        if task_id is None:
            # 同 head 已被另一生产者认领（排队/在审）→ 幂等跳过，防止并发重复审查、重复
            # 刷评论；终态（failed）不在此列，会拿到 id 重试。与 push 轨同语。
            return "already"
        # 开审即标 running（DESIGN §9.2）：让「正在跑」与「排队/孤儿」在管理页可区分；
        # 后续 failed/completed 的 mark_state 会覆盖。
        await review_repo.mark_state(task_id, state="running")

    # 原始 LLM 对话采集器：agentic/diff 的每条 LLM 调用经 contextvar 注入即时落库
    #（对话落库失败已被 recorder 吞掉记 warning，绝不阻断审查主链）。
    recorder: ConversationRecorder | None = None
    if agent_conversation_enabled and engine is not None and task_id is not None:
        recorder = ConversationRecorder(engine, task_id=task_id, trace_id=TRACE_ID.get())

    try:
        # 项目配置提前取：MR 门控与扩展名过滤共用一次查询（配置改动实时生效）
        cfg = await _project_cfg(project_config_factory, pr.provider, pr.repo_id)
        # —— MR 轨自动审查门控（与 push 对称：全局默认 → 项目覆盖；手动补审 force_rerun 绕过）——
        # mr_default_enabled 在 main 生产总传 settings.mr_review_enabled（默认关）；未接线
        # （None，存量调用/测试）不门控，保持原自动审语义。
        if mr_default_enabled is not None:
            enabled = mr_default_enabled
            if engine is not None:
                db_default = await SettingRepository(engine) \
                    .get_bool_optional(MR_REVIEW_DEFAULT_KEY)
                if db_default is not None:
                    enabled = db_default
            if cfg is not None and cfg.mr_enabled is not None:
                enabled = cfg.mr_enabled
            force = False
            if review_repo is not None and task_id is not None:
                force = await review_repo.force_rerun_flag(task_id)
                if force:
                    await review_repo.clear_force_rerun(task_id)
            if force:
                enabled = True
            if not enabled:
                # 未开启：审计行标 skipped，带原因；门控/配置类跳过可由前端「重试」补审
                if review_repo is not None and task_id is not None:
                    await review_repo.mark_state(
                        task_id, state="skipped", skip_reason="mr_disabled",
                        error="MR 自动审查未开启（默认关闭），仅记录未审查",
                    )
                return "skipped"
        refreshed = await forge.fetch_pull_request(pr)  # 补 diff_refs（行级评论 position 必填）
        diffs = await forge.fetch_files(refreshed)
        # 项目级文件扩展名过滤：只审命中的文件（DESIGN 文件扩展名过滤）
        if cfg and cfg.file_extensions:
            diffs = apply_extension_filter(diffs, cfg.file_extensions)
        # 未变更文件复用：增量轮里内容哈希未变的文件不再喂 agent，直接复用上次
        #「已审过干净」的结果（省 token/时，对齐 OCR review_item_reused）。
        if (incremental and agent_reuse_enabled and review_repo is not None
                and ref is not None):
            last_covered = await review_repo.last_covered(
                pr.provider, pr.repo_id, pr.pr_number)
            diffs = prune_unchanged(diffs, last_covered)
        if not diffs or _count_diff_lines(diffs) == 0:
            # 无待审文件 / 实变行数为 0（空 diff、重命名、无 +/- 变更）→ 不调 LLM
            #（省 token、避免空输入把 LLM 逼出空返回再落 failed）；标 completed-empty
            if task_id is not None:
                await review_repo.mark_state(task_id, state="completed",
                                             summary_md="_无待审文件（扩展名过滤、全部未变更或空 diff）_",
                                             score_total=0)
            return "empty"
        # 项目级 review_strategy 覆盖全局默认：页面/每个项目选的 agentic/diff 真正生效
        if cfg and cfg.review_strategy:
            review_strategy = cfg.review_strategy
        # 静态分析先跑（DESIGN §11）：失败降级为空，不影响主链
        static_findings = await _run_static(static_analyzer, diffs)
        # 审查的 LLM 调用经 contextvar 采集进 review_conversation（adapter 读到 recorder 即采）
        async with conversation_capture(recorder):
            # diff 模式不走对话采集 → 通道 gateway 回调落 ModelUsage，看板 Token/成本能按模式拆分
            usage_sink = diff_usage_sink(engine, task_id) \
                if engine is not None and task_id is not None else None
            result, exec_mode = await _review_agent_or_diff(
                reviewer, grouper, pr=refreshed, commits_text=refreshed.title, diffs=diffs,
                static_findings=static_findings, strategy=review_strategy,
                agent_runtime=agent_runtime, agent_llm_factory=agent_llm_factory,
                agent_config=agent_config, usage_sink=usage_sink,
            )

        if incremental:
            # 只审增量：按内容指纹滤掉上次已报过的 finding（DESIGN §7.3 / §13.3）
            result.findings = dedup_findings(result.findings, ref)

        # ── 成果先落库（兜底方案 1+6：DESIGN §9.2）──────────────────────────
        # findings 持久化放在回写之前：即使网络回写失败，审查成果也不丢失，
        # 可从前端「重新发送」用已持久化内容补齐评论，**不重算**昂贵 agentic 审查。
        if review_repo is not None and task_id is not None:
            # mr 轨真落库（幂等；同 head 已存在则跳过，不重复写）
            # 非增量全量轮：finding 生命周期对账（DESIGN §7.3）。
            # 缺席→resolved / 复现→回 active+reopened；复现指纹返回作 skip 去重。
            skip: frozenset[str] = frozenset()
            if not incremental and ref is not None:
                covered = {p for d in diffs for p in (d.old_path, d.new_path)}
                skip = await review_repo.reconcile_findings(
                    provider=pr.provider, repo_id=pr.repo_id, pr_number=pr.pr_number,
                    current_findings=result.findings, covered_files=covered,
                    exclude_task_id=task_id,
                )
            await review_repo.insert_findings(task_id, result.findings, skip_fingerprints=skip)
            # 本轮覆盖集（new_path→sha1(new)）落 diff_snapshot：供下轮未变更文件复用
            # 与 /compare 的 not_reviewed 判定。
            await review_repo.set_coverage(task_id, covered_file_map(diffs))
            # 执行态四列快照（exec_mode=实际路径；chat/tool 轮数取对话采集累计）。
            # 放在成果落库处：即使后续回写失败，执行态也已记下，不随回写重算。
            chat_rounds, tool_calls = _exec_metrics(recorder)
            await review_repo.set_exec_metrics(
                task_id, exec_mode=exec_mode,
                diff_lines=_count_diff_lines(diffs), chat_rounds=chat_rounds, tool_calls=tool_calls,
            )

        # ── 回写 forge：失败不重算，落 writeback_failed 供前端重发 ───────────
        writeback_failed = False
        fingerprint = review_fingerprint(
            provider=pr.provider, repo_id=pr.repo_id,
            pr_number=pr.pr_number, head_sha=refreshed.head_sha,
        )
        try:
            await ResultWriter(forge).write(
                refreshed, diffs, result, fingerprint=fingerprint,
            )
        except Exception as exc:  # noqa: BLE001
            # 回写失败：成果已落库，不重算；打标供前端「重新发送」补齐，任务保持
            # completed 展示、不触发粒度重试（避免整套 agentic 审查被重做）。
            logger.warning(
                "回写失败（%s pr#%s，成果已落库）：%s",
                pr.repo_full_name, pr.pr_number, exc,
            )
            writeback_failed = True
            if review_repo is not None and task_id is not None:
                try:
                    await review_repo.mark_writeback(task_id, True)
                    await review_repo.mark_state(
                        task_id, state="completed", summary_md=result.summary,
                        score_total=result.scores.total,
                        error="回写失败，可点任务行「重新发送」补齐评论",
                    )
                except Exception:
                    pass  # 打标失败不遮蔽原始回写异常
            # 人工兜底（方案 7）：IM 显式补发提示，fire-and-forget 失败不炸主链
            if notifier is not None:
                asyncio.create_task(
                    notifier.send_markdown(
                        "⚠️ 审查完成但回写失败",
                        "AI 审查已完成并落库，但评论回写 GitHub/GitLab 失败。\n"
                        "请到任务列表对该任务点「重新发送」补齐评论。",
                    )
                )
        else:
            if review_repo is not None and task_id is not None:
                # 回写成功才标 completed（保持「completed 只由成功达成」语义）
                await review_repo.mark_state(
                    task_id, state="completed", summary_md=result.summary,
                    score_total=result.scores.total,
                )

        if (not writeback_failed and cfg
                and cfg.enforce_score_threshold and refreshed.head_sha):
            # F3.7：评分卡 CI status——低于阈值发 failed（阻塞合并），达标发 success。
            # 与 notifier 同哲学：网络失败仅告警、不标 failed、不影响审查本身。
            # 回写失败时跳过：评论未发出，CI 状态无意义，随下次重发一并补齐。
            passed = result.scores.total >= cfg.score_threshold
            try:
                await forge.post_commit_status(
                    refreshed, passed=passed,
                    description=f"AI 审查 {result.scores.total}/100",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "写 commit status 失败（%s pr#%s）：%s",
                    pr.repo_full_name, pr.pr_number, exc,
                )

        if notifier is not None:
            # F4.4 fire-and-forget：推送后台化，不拖慢也不阻断审查主链
            notifier.launch(refreshed, result)

        if increments is not None and refreshed.head_sha:
            # 内存档才显式记录落点；DB 档 findigs 已落 review_finding，由 review_repo 读取
            increments.record(
                pr.provider, pr.pr_number, refreshed.head_sha, collect_fingerprints(result.findings)
            )
        return "reviewed"
    except Exception as exc:
        # 失败落 failed 行（后台可见、可重试），再向上抛出由 worker 标队列 failed
        logger.warning("mr 轨审查失败（%s pr#%s）：%s", pr.repo_full_name, pr.pr_number, exc)
        if task_id is not None:
            try:
                await review_repo.mark_state(task_id, state="failed", error=str(exc))
            except Exception:
                pass  # 落库失败不遮蔽原始异常
        raise


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
    agent_config: AgentConfig | None = None,
    engine: AsyncEngine | None = None,
    agent_conversation_enabled: bool = True,
    agent_reuse_enabled: bool = True,
    project_config_factory: ProjectConfigFactory | None = None,
    mr_default_enabled: bool | None = None,
) -> None:
    """原始 webhook payload → 审查 + 回写。各阶段失败在此抛出，由 worker 标 failed。
    `mr_default_enabled`：MR 轨自动审查全局默认（main 总传 settings.mr_review_enabled）；
    None（未接线）则 MR 不门控、保持自动审（存量调用/测试）。

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
        setting_repo = SettingRepository(engine) if engine is not None else None
        await _review_push_event(
            forge, reviewer, ev, review_repo=review_repo, grouper=grouper,
            notifier=notifier, push_gate=push_gate, static_analyzer=static_analyzer,
            review_strategy=review_strategy, agent_runtime=agent_runtime,
            agent_llm_factory=agent_llm_factory, agent_config=agent_config,
            project_config_factory=project_config_factory, setting_repo=setting_repo,
            raw_payload=raw.decode("utf-8", "replace"), engine=engine,
        )
        return
    if not forge.should_review(_event_action(data)):
        return  # close/merge 等动作不触发审查

    # mr 轨核心（webhook 与主动补拉共用）：增量决策 / fetch / 过滤 / 审查 / 回写。
    # 主动补拉（ops.poller）直接构造 PullRequest 后也走这里，幂等由 head_sha 兜底。
    await review_pull_request(
        forge, reviewer, pr,
        increments=increments, review_repo=review_repo, grouper=grouper,
        chain_valid=chain_valid, notifier=notifier, static_analyzer=static_analyzer,
        review_strategy=review_strategy, agent_runtime=agent_runtime,
        agent_llm_factory=agent_llm_factory, agent_config=agent_config,
        engine=engine, agent_conversation_enabled=agent_conversation_enabled,
        agent_reuse_enabled=agent_reuse_enabled,
        project_config_factory=project_config_factory,
        raw_payload=raw.decode("utf-8", "replace"),
        mr_default_enabled=mr_default_enabled,
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
    agent_config: AgentConfig | None = None,
    project_config_factory: ProjectConfigFactory | None = None,
    setting_repo: SettingRepository | None = None,
    raw_payload: str = "",
    engine: AsyncEngine | None = None,
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
        # push 直达链接：拼「{项目仓库 web_url}/commit/{head_sha}」（项目未配 web_url → 留空，详情页隐藏）
        push_url = f"{cfg.web_url}/commit/{ev.after}" if cfg and cfg.web_url else ""
        tid = await review_repo.ensure_task(
            provider=ev.provider, repo_id=ev.repo_id, pr_number=None, event_type="push",
            branch=ev.branch, head_sha=ev.after, base_sha=ev.before,
            # push 无 PR 标题；提交消息多行过长，不当标题占 pr_title/表格列，落 push_commits 详情展示
            pr_title="", web_url=push_url, push_commits=_commits_text(ev), payload=raw_payload,
        )
        if tid is None:
            return  # 并发下另一 worker 抢先插入 → 幂等跳过（§7.7）
        audit_id = tid
        # 开审即标 running（DESIGN §9.2）：区分「正在跑」与「排队/孤儿」；
        # 门控 skipped / 删分支分支随后会覆盖。
        await review_repo.mark_state(audit_id, state="running")
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
        # 门控解析：enabled / branch_match = 全局默认 → 项目显式覆盖（cfg）
        # 全局默认 = app_setting `push_review_default`（后台「自动审查触发」可热更）
        #   → 无落库行则回落到 env/push_gate；项目 push_enabled 仍可单独覆盖
        enabled = push_gate.enabled if push_gate is not None else False
        branch_match = push_gate.branch_match if push_gate is not None else None
        if setting_repo is not None:
            db_default = await setting_repo.get_bool_optional(PUSH_REVIEW_DEFAULT_KEY)
            if db_default is not None:
                enabled = db_default
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
        if not diffs or _count_diff_lines(diffs) == 0:
            # 无待审文件 / 实变行数为 0（空 diff/重命名）→ 不调 LLM，审计行标 completed-empty
            if review_repo is not None:
                await review_repo.mark_state(audit_id, state="completed",
                                             summary_md="_扩展名过滤或无待审变更_", score_total=0)
            return
        pr = _push_as_pr(ev)
        static_findings = await _run_static(static_analyzer, diffs)
        usage_sink = diff_usage_sink(engine, audit_id) \
            if engine is not None and audit_id else None
        result, exec_mode = await _review_agent_or_diff(
            reviewer, grouper, pr=pr, commits_text=_commits_text(ev), diffs=diffs,
            static_findings=static_findings, strategy=review_strategy,
            agent_runtime=agent_runtime, agent_llm_factory=agent_llm_factory,
            agent_config=agent_config, usage_sink=usage_sink,
        )
        summary = build_push_summary(ev, result)
        await forge.post_commit_summary(ev, summary)  # 只一条总结评论，无行级（§7.7）
        if notifier is not None:
            notifier.launch(pr, result)
        if review_repo is not None:
            await review_repo.insert_findings(audit_id, result.findings)
            # push 轨当前不采集对话，chat/tool 轮数记 0；exec_mode/diff_lines 照实落库
            await review_repo.set_exec_metrics(
                audit_id, exec_mode=exec_mode,
                diff_lines=_count_diff_lines(diffs), chat_rounds=0, tool_calls=0,
            )
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
    agent_config: AgentConfig | None = None,
    engine: AsyncEngine | None = None,
    agent_conversation_enabled: bool = True,
    agent_reuse_enabled: bool = True,
    project_config_factory: ProjectConfigFactory | None = None,
    mr_default_enabled: bool | None = None,
) -> Callable[[TaskMeta], Awaitable[None]]:
    """由 worker 主循环调用的处理函数：根据 task 取 payload 后走完整管线。
    `mr_default_enabled` 透传给 MR 轨（webhook 与补拉直通都过 `_do_review_pull_request` 门控）。

    `increments`/`review_repo`/`grouper`/`chain_valid`/`notifier`/`push_gate`/
    `static_analyzer`/`review_strategy`/`agent_runtime`/`agent_llm_factory`/
    `agent_config`/`project_config_factory` 透传给审查链（都缺省时禁用增量/分组/推送/
    记录/静态分析/沙箱/扩展名过滤，见其 docstring）。
    """

    async def process(task: TaskMeta) -> None:
        item = store.get(task.task_id)
        if item is None:
            return  # 无暂存 payload（如直接入队的调试任务），视为已处理
        provider, payload = item
        forge = forge_factory(provider)
        reviewer = reviewer_factory(provider)
        if forge is None:
            logger.warning("provider %s 未配置适配器，任务 %s 跳过", provider, task.task_id)
            return
        if isinstance(payload, PullRequest):
            # 补拉入队的已解析 PR → 直接跑 mr 轨核心（无需再 parse 原始 body）。
            # 不转发 push_gate（review_pull_request 不接）；其余与 process_raw_event 一致。
            await review_pull_request(
                forge,
                reviewer,
                payload,
                increments=increments,
                review_repo=review_repo,
                grouper=grouper,
                chain_valid=chain_valid,
                notifier=notifier,
                static_analyzer=static_analyzer,
                review_strategy=review_strategy,
                agent_runtime=agent_runtime,
                agent_llm_factory=agent_llm_factory,
                agent_config=agent_config,
                engine=engine,
                agent_conversation_enabled=agent_conversation_enabled,
                agent_reuse_enabled=agent_reuse_enabled,
                project_config_factory=project_config_factory,
                raw_payload="",
                mr_default_enabled=mr_default_enabled,
            )
            return
        await process_raw_event(
            forge,
            reviewer,
            payload,
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
            agent_config=agent_config,
            engine=engine,
            agent_conversation_enabled=agent_conversation_enabled,
            agent_reuse_enabled=agent_reuse_enabled,
            project_config_factory=project_config_factory,
            mr_default_enabled=mr_default_enabled,
        )

    return process

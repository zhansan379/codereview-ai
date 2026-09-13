"""审查 worker 运行时装配（懒启动）：条件齐备即现场拉起，不要求重启服务。

原先「有无可用 worker」在启动时一次性定格（main.py lifespan）：启动时没配 LLM 的
话，之后在设置页把模型/平台凭据配齐也不生效，必须重启进程——对桌面版单进程部署
极不友好，也催生「请配置后重启服务再执行」的糟糕体验。这里把 worker 装配块抽成
幂等的 `ensure_worker_started`：

- 每次调用实时解析 DB/env 配置（`build_reviewer` + `forge_registry.available()`），
  不齐备返回 False 且不留副作用；齐备则装配 notifier/push 门控/静态分析/agentic/
  processor/worker 池/调度器，并跑一遍启动回放（遗留 queued/running 续跑）。
- 幂等 + 并发安全：已在跑（`app.state.worker_pool` 存在）直接返回 True；并发触发
  由 app 级 asyncio.Lock 串行化，只装配一次。
- `can_run` 判据统一为「worker_pool 是否存在」（动态）：worker 拉起的瞬间，
  webhook/补拉的 hold 自动解除、恢复正常入队，无需重启。

触发点：main.py 启动时试拉一次（行为与旧一次性装配等价）、执行端点 409 前兜底、
模型/平台凭据保存后调用（配好即生效）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import httpx

from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.ops.periodic import DailyReporter
from codereview_ai.ops.poller import PRPoller
from codereview_ai.ops.scheduler import ScheduleManager
from codereview_ai.queue.concurrency import WorkerPool
from codereview_ai.review.static_analysis import StaticAnalyzer
from codereview_ai.storage.clone_cache_repo import CloneCacheRepoRepository
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.project_repo import ProjectRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.storage.setting_repo import SettingRepository
from codereview_ai.worker import PushGate, make_processor, replay_pending_tasks

logger = logging.getLogger("codereview_ai.ops.bootstrap")


def _branch_glob_match(globs: str) -> Callable[[str], bool] | None:
    """把 `CR_PUSH_BRANCH_GLOBS`（逗号分隔 fnmatch）编译成 `branch -> bool`；空则 None。"""
    patterns = [p.strip() for p in globs.split(",") if p.strip()]
    if not patterns:
        return None

    def match(branch: str) -> bool:
        return any(fnmatch(branch, p) for p in patterns)

    return match


def worker_ready(app: Any) -> bool:
    """服务内是否已启动审查 worker（动态判据：worker_pool 是否存在）。"""
    return getattr(app.state, "worker_pool", None) is not None


def worker_can_run(app: Any) -> Callable[[], bool]:
    """给 enqueuer/poller 的动态 `can_run`：worker 在 → 正常入队；不在 → hold 落未开始。"""
    return lambda: worker_ready(app)


def get_or_create_poller(app: Any) -> PRPoller | None:
    """取现成补拉器，缺则按当前配置创建一个（启动/手动补拉/调度共用，幂等）。

    平台凭据不可用（registry 缺失或未配齐）返回 None，调用方自行报「不可用」。
    `can_run` 用动态判据：worker 懒启动后同一 poller 即恢复入队，不持有过期快照。
    """
    poller: PRPoller | None = getattr(app.state, "poller", None)
    if poller is not None:
        return poller
    registry = getattr(app.state, "forge_registry", None)
    enqueuer = getattr(app.state, "enqueuer", None)
    settings = getattr(app.state, "settings", None)
    engine = getattr(app.state, "engine", None)
    if (registry is None or not registry.available() or enqueuer is None
            or settings is None or engine is None):
        return None
    created = PRPoller(
        engine, registry, enqueuer,
        include_closed_default=settings.poll_include_closed,
        can_run=worker_can_run(app),
    )
    app.state.poller = created
    app.state.poll_running = False
    app.state.poll_last = None
    app.state.poll_error = None
    app.state.poll_run_task = None
    return created


async def ensure_worker_started(app: Any) -> bool:
    """条件齐备则装配并启动审查 worker（幂等、可并发触发）；返回 worker 是否就绪。

    装配顺序有讲究：先落 `app.state.worker_pool`（`can_run` 动态判据随之翻转），
    **再**跑启动回放——回放经 `enqueuer.enqueue` 入队要过 `can_run`，若在翻转前
    回放会把遗留 queued 行误判成 hold 洗成「未开始」。
    """
    engine = getattr(app.state, "engine", None)
    settings = getattr(app.state, "settings", None)
    provider_repo = getattr(app.state, "config_repository", None)
    registry = getattr(app.state, "forge_registry", None)
    enqueuer = getattr(app.state, "enqueuer", None)
    queue = getattr(app.state, "queue", None)
    store = getattr(app.state, "event_store", None)
    if (engine is None or settings is None or provider_repo is None or registry is None
            or enqueuer is None or queue is None or store is None):
        return False

    lock = getattr(app.state, "worker_boot_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.worker_boot_lock = lock
    async with lock:
        if worker_ready(app):
            return True
        reviewer = await provider_repo.build_reviewer()
        if reviewer is None or not registry.available():
            return False

        cache_root = getattr(app.state, "cache_root", None) or str(
            Path(gettempdir()) / "codereview-agent-repos"
        )
        http: httpx.AsyncClient | None = getattr(app.state, "http", None)
        if http is None:
            http = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
            app.state.http = http

        async def _record_cache_sync(
            *, key: str, provider: str, repo_full_name: str,
            url: str, local_path: str, head_sha: str,
        ) -> None:
            """每次 agent 克隆同步成功后登记/刷新一行「最近拉取」注册记录。"""
            session = session_factory(engine)
            async with session() as s:
                await CloneCacheRepoRepository(s).upsert_fetched(
                    key, provider=provider, repo_full_name=repo_full_name,
                    url=url, local_path=local_path, head_sha=head_sha,
                )

        notifier = NotifierDispatcher(
            provider_repo.notifier_routes,
            http=http,
            resolve_member=provider_repo.resolve_member_by_git_username,
        )
        # §7.7：push 轨默认关；由 env 开关 + 分支 glob 构造 PushGate
        push_gate = PushGate(
            enabled=settings.push_review_enabled,
            branch_match=_branch_glob_match(settings.push_branch_globs),
        )
        # §11 静态分析：默认关；配置页「静态分析」开关（app_setting）每任务热读可覆盖，
        # 缺工具自动降级，不影响主链
        ws = Path(settings.static_workspace_dir) if settings.static_workspace_dir else None
        rules = Path(settings.semgrep_rules_dir) if settings.semgrep_rules_dir else None
        static_analyzer = StaticAnalyzer(
            enabled=settings.review_static_enabled, workspace=ws, semgrep_rules=rules,
        )
        # 项目级配置（文件扩展名过滤）：按 (provider, repo_id) 实时读 project 启用行
        project_repo = ProjectRepository(engine)
        # §12 agentic 审查：全局开 + 有可用 LLM 才接线运行时与工具工厂；否则留 None
        agent_runtime = agent_llm_factory = agent_config = None
        grouper = None  # §7.2 LLM 语义分组；agentic 未启用或无 LLM 时留 None
        if settings.agent_review_enabled:
            async def _agent_token_for(provider: str) -> str | None:
                try:
                    resolved = await provider_repo.resolve_forge(provider)
                except Exception:  # noqa: BLE001 —— token 临时取不到就让 clone 走公开地址
                    return None
                return resolved.token if resolved else None

            agent_resolved = await provider_repo.resolve_llm()
            if agent_resolved and agent_resolved.model:
                from codereview_ai.review.agentic.llm_adapter import build_agent_llm
                from codereview_ai.review.agentic.llmloop import AgentConfig
                from codereview_ai.review.agentic.sandbox import LocalCloneRuntime

                agent_runtime = LocalCloneRuntime(cache_root=cache_root, enabled=True,
                                                  token_for=_agent_token_for,
                                                  record_sync=_record_cache_sync)
                # 会话上限（轮数/时长/预算）从 env（CR_AGENT_*）读取，单组独立会话生效
                agent_config = AgentConfig(
                    max_iterations=settings.agent_max_iterations,
                    max_time_seconds=settings.agent_max_time_seconds,
                    max_prompt_tokens=settings.agent_max_prompt_tokens,
                    group_concurrency=settings.agent_group_concurrency,
                    plan_enabled=settings.agent_plan_enabled,
                    relocation_enabled=settings.agent_relocation_enabled,
                    review_filter_enabled=settings.agent_review_filter_enabled,
                    scoring_enabled=settings.agent_scoring_enabled,
                    plan_line_threshold=settings.agent_plan_line_threshold,
                    plan_group_line_threshold=settings.agent_plan_group_line_threshold,
                )

                def _agent_llm_factory() -> Any:
                    # 每次分组重建一个独立会话（独立 trace_id），拿同一根解析配置
                    return build_agent_llm(agent_resolved)

                agent_llm_factory = _agent_llm_factory
                # §7.2 LLM 语义分组：复用 agent 同一根 LLM 档位做一次最便宜元数据调用
                from codereview_ai.review.group_gateway import LLMGroupAdapter
                from codereview_ai.review.grouping import SemanticGrouper
                from codereview_ai.review.llm_gateway import LLMGateway

                grouper = SemanticGrouper(LLMGroupAdapter(LLMGateway(
                    model=agent_resolved.model,
                    api_key=agent_resolved.api_key,
                    base_url=agent_resolved.base_url,
                    max_tokens=agent_resolved.max_tokens,
                    temperature=agent_resolved.temperature,
                    json_object=True,
                )))
                logger.info("agentic 审查已启用（clone 缓存：%s，轮数=%d）",
                            cache_root, settings.agent_max_iterations)
            else:
                # 全局开关开但无可用 LLM：worker 三条件不满足自动走 diff 降级
                logger.warning("agent_review_enabled 开启但无可用 LLM，agentic 走 diff 降级")

        processor = make_processor(
            lambda p: registry.get(p), lambda _: reviewer, store,
            review_repo=ReviewRepository(engine),
            notifier=notifier, push_gate=push_gate, static_analyzer=static_analyzer,
            engine=engine,
            agent_runtime=agent_runtime, agent_llm_factory=agent_llm_factory,
            agent_config=agent_config,
            grouper=grouper,
            agent_conversation_enabled=settings.agent_conversation_enabled,
            agent_reuse_enabled=settings.agent_reuse_enabled,
            project_config_factory=project_repo.config_for,
            mr_default_enabled=settings.mr_review_enabled,
        )
        # 并发审查：固定数量 worker 循环 + 并发闸（上限可热更、落 DB 保留）
        init_concurrency = await SettingRepository(engine).get_int(
            "worker_concurrency", settings.max_concurrent_reviews
        )
        # M5.7 日报 + §9 补拉：统一由 ScheduleManager 按 schedule_job 表驱动（落 DB + 热更）
        reporter = DailyReporter(engine, notifier)
        scheduler = ScheduleManager(
            engine, poller=get_or_create_poller(app), reporter=reporter,
            seed_defaults={
                "poll": {
                    "enabled": settings.poll_enabled,
                    "params": {"interval_seconds": settings.poll_interval_seconds},
                },
                "daily": {
                    "enabled": settings.daily_report_enabled,
                    "params": {"hour": settings.daily_report_hour},
                },
            },
        )
        await scheduler.start()
        app.state.scheduler = scheduler
        app.state.worker_pool = WorkerPool(queue, processor, limit=init_concurrency)
        logger.info(
            "审查 worker 已就绪（懒启动自检通过）：%s（model %s）",
            ",".join(registry.providers()),
            reviewer.gateway.model,
        )
        # —— 启动回放（DESIGN §9.2 / 崩溃恢复）：遗留 queued/running 重新投回内存队列
        #    自动续跑（幂等：已审同 head 被增量决策短路）。懒启动同样补跑，把之前
        #    「无 worker 时期」卡死的 queued 行救活。
        try:
            review_repo = ReviewRepository(engine)
            pending_rows = await review_repo.pending_for_replay()
            n_replay = await replay_pending_tasks(review_repo, enqueuer, pending_rows)
            if n_replay:
                logger.info("启动回放：重新入队 %s 条遗留任务续跑", n_replay)
        except Exception:  # noqa: BLE001 回放失败不影响 worker 就绪；行仍在可再次重启回放
            logger.exception("启动回放失败（worker 已就绪，遗留任务可在重启后重放）")
        return True

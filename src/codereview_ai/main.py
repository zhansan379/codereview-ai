"""FastAPI 入口（DESIGN §1）。

- 生命周期：结构化日志 → 建 engine（SQLite 加固）→ 建表 → 暴露到 `app.state` 供 /ready 探测。
- 挂 trace 中间件与健康路由；OpenAPI 文档可由 `CR_OPENAPI_ENABLED=0` 关闭。
- 顶部 `app = create_app()` 在 import 时即构造 `Settings`，缺密钥会 fail-fast 退出。
- webhook 路由挂载后，把 simple 档的 enqueuer 注入 `app.state`；若平台/LLM 配置齐备，
  额外启动内置 worker 消费队列（审计编排见 worker.py）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from pathlib import Path

import httpx
from fastapi import FastAPI

from codereview_ai.api.admin import (
    clone_cache,
    forges,
    models,
    notifiers,
    projects,
    pull,
    reviews,
    roles,
    schedules,
    stats,
    tasks,
    usage,
    users,
)
from codereview_ai.api.admin import (
    settings as admin_settings,  # 全局运行时设置（并发数）
)
from codereview_ai.api.admin.notifier_members import router as members  # 系统级 @成员名单
from codereview_ai.api.admin_ui import mount_admin
from codereview_ai.api.auth import router as auth_router
from codereview_ai.api.webhook import router as webhook_router
from codereview_ai.config import Settings
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.logging import setup_logging
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.ops.clone_cache import CloneCachePruner
from codereview_ai.ops.health import router as health_router
from codereview_ai.ops.periodic import DailyReporter
from codereview_ai.ops.poller import PRPoller
from codereview_ai.ops.scheduler import ScheduleManager
from codereview_ai.ops.tracing import TraceMiddleware
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.concurrency import WorkerPool
from codereview_ai.review.static_analysis import StaticAnalyzer
from codereview_ai.security_guard import LoginGuard
from codereview_ai.storage.clone_cache_repo import CloneCacheRepoRepository
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.project_repo import ProjectRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.storage.seed import (
    ensure_member_role,
    ensure_workspace_backfill,
    prune_obsolete_permissions,
    seed_rbac,
    sync_permission_catalog,
)
from codereview_ai.storage.setting_repo import SettingRepository
from codereview_ai.worker import (
    EventStore,
    PushGate,
    QueueEnqueuer,
    make_processor,
    replay_pending_tasks,
    scribble_queued_task,
)

logger = logging.getLogger("codereview_ai.main")


def _branch_glob_match(globs: str) -> Callable[[str], bool] | None:
    """把 `CR_PUSH_BRANCH_GLOBS`（逗号分隔 fnmatch）编译成 `branch -> bool`；空则 None。"""
    patterns = [p.strip() for p in globs.split(",") if p.strip()]
    if not patterns:
        return None

    def match(branch: str) -> bool:
        return any(fnmatch(branch, p) for p in patterns)

    return match


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(log_level=settings.log_level)
        engine = create_engine(settings.database_url)
        await init_db(engine)
        app.state.engine = engine
        app.state.settings = settings
        # —— 阶段 D：登录限速 + 验证码守卫（内存实例，测试可 reset）——
        app.state.login_guard = LoginGuard(
            login_rate_attempts=settings.login_rate_limit_attempts,
            login_rate_window_seconds=settings.login_rate_limit_window_seconds,
            captcha_threshold_attempts=settings.captcha_threshold_attempts,
            captcha_ttl_seconds=settings.captcha_ttl_seconds,
        )

        # —— agent 本地克隆缓存：注册表 + 清除策略后台（独立于 agentic 开关，列表/清理恒可用）——
        cache_root = settings.agent_clone_cache_dir or str(
            Path(tempfile.gettempdir()) / "codereview-agent-repos"
        )
        app.state.cache_root = cache_root

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

        pruner = CloneCachePruner(engine, cache_root)
        await pruner.start()
        app.state.pruner = pruner

        # —— RBAC 种子（F5.11）：MLP 就绪后播种权限目录 + 内置角色 + 首个 admin ——
        async with session_factory(engine)() as s:
            # 对账权限目录↔permission 表：新增码补行、删掉码清残留（均为幂等 no-op 兜底）
            await sync_permission_catalog(s)
            await prune_obsolete_permissions(s)
            await seed_rbac(s, settings.admin_password)
            await ensure_member_role(s)            # 存量库也补自助注册默认角色
            await ensure_workspace_backfill(s)     # 存量项目归入默认工作区（幂等）

        # —— simple 档队列：webhook 入队即返回 202，worker 异步消费 ——
        store = EventStore()
        queue = AsyncioTaskQueue()
        app.state.queue = queue
        app.state.enqueuer = QueueEnqueuer(queue, store)
        scheduler: ScheduleManager | None = None
        poll: PRPoller | None = None
        http: httpx.AsyncClient | None = None

        # —— DB 驱动配置（DESIGN §16）：LLM 模型/api_key 从 DB 解析，env 重放压 DB
        provider_repo = ConfigRepository(engine, encryption_key=settings.encryption_key)
        reviewer = await provider_repo.build_reviewer()
        app.state.config_repository = provider_repo
        # —— 平台适配器：DB/env 解析 + 保存后热更（ForgeRegistry）——
        http = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        forge_registry = ForgeRegistry(provider_repo, http)
        await forge_registry.refresh_all()
        app.state.forge_registry = forge_registry
        if reviewer is not None and forge_registry.available():
            review_repo = ReviewRepository(engine)
            # —— 入队即建 mr 审计行（DESIGN §9.2）：让队列里等待的 PR 从入队起就可见为
            #    『排队中』，而非开审才建行（长任务排队时不可见）。幂等，已审同 head 短路。
            async def _on_enqueue(provider: str, raw: bytes) -> None:
                await scribble_queued_task(review_repo, forge_registry.get(provider), raw)

            app.state.enqueuer.on_enqueue = _on_enqueue
            # —— 启动回放（DESIGN §9.2 / 崩溃恢复）：重启后把遗留卡死的 queued/running
            #    且带 payload 的任务重新投回内存队列自动续跑（幂等：已审过的同 head 会被
            #    增量决策短路，不重复审查）。否则数据库里的 queued 行将永远孤死无失败原因。
            n_replay = await replay_pending_tasks(review_repo, app.state.enqueuer)
            if n_replay:
                logger.info("启动回放：重新入队 %s 条遗留任务续跑", n_replay)
            # F4/M4.7：路由从 DB notifier_config 拉取（project_id=None→仅全局默认）
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
            # §11 静态分析：默认开，缺工具自动降级，不影响主链。semgrep 默认 registry-first、
            # 失败内置离线包兜底；显式配 CR_SEMGREP_RULES 则完全离线走本地目录
            ws = Path(settings.static_workspace_dir) if settings.static_workspace_dir else None
            rules = Path(settings.semgrep_rules_dir) if settings.semgrep_rules_dir else None
            static_analyzer = StaticAnalyzer(
                enabled=settings.review_static_enabled, workspace=ws, semgrep_rules=rules,
            )
            # 项目级配置（文件扩展名过滤）：按 (provider, repo_id) 实时读 project 启用行
            project_repo = ProjectRepository(engine)
            # §12 agentic 审查：全局开 + 有可用 LLM 才接线运行时与工具工厂；否则留 None
            # （worker 三条件不满足自动走 diff，不在这里抛错）。async token 回调按 provider
            # 实时取平台 token 供 clone，DB/env 热更即时生效。
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

                    def _agent_llm_factory():
                        # 每次分组重建一个独立会话（独立 trace_id），拿同一根解析配置
                        return build_agent_llm(agent_resolved)

                    agent_llm_factory = _agent_llm_factory
                    # §7.2 LLM 语义分组：复用 agent 同一根 LLM 档位做一次最便宜元数据调用，
                    # 让 ≥4 文件的大变更真走分组并发；失败/不可行由 SemanticGrouper 降级 per-file。
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
                lambda p: forge_registry.get(p), lambda _: reviewer, store, review_repo=review_repo,
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
            # 并发审查：固定数量 worker 循环 + 并发闸（上限可热更、落 DB 保留，
            # 见 /settings/concurrency）
            init_concurrency = await SettingRepository(engine).get_int(
                "worker_concurrency", settings.max_concurrent_reviews
            )
            worker_pool = WorkerPool(queue, processor, limit=init_concurrency)
            app.state.worker_pool = worker_pool
            # M5.7 日报 + §9 补拉：统一由 ScheduleManager 按 schedule_job 表驱动（落 DB + 热更）
            reporter = DailyReporter(engine, notifier)
            # 补拉只发现+落 queued 行+入队到 worker 队列异步审查，递 enqueuer 即可。
            poll = PRPoller(engine, forge_registry, app.state.enqueuer)
            app.state.poller = poll
            # 手动补拉的后台任务状态（POST /pulls/poll → run_once 放入后台，/status 读取）
            app.state.poll_running = False
            app.state.poll_last = None
            app.state.poll_error = None
            app.state.poll_run_task = None
            scheduler = ScheduleManager(
                engine, poller=poll, reporter=reporter,
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
            logger.info(
                "内置 worker 已启动：%s（model %s）",
                ",".join(forge_registry.providers()),
                reviewer.gateway.model,
            )
        else:
            logger.warning(
                "未配置可用 LLM 模型（env CR_LLM_MODEL / DB model_config）或可用平台"
                "（env CR_GITHUB_TOKEN/CR_GITLAB_TOKEN 或设置页 DB），webhook 仍可入队但无 worker"
            )

        app_port = os.environ.get("CR_APP_PORT", "5001")
        logger.info("管理后台已就绪：http://127.0.0.1:%s/admin", app_port)

        try:
            yield
        finally:
            local_pruner = getattr(app.state, "pruner", None)
            if local_pruner is not None:
                await local_pruner.stop()  # 停清除策略后台循环
            if scheduler is not None:
                await scheduler.stop()  # 停定时任务（日报/补拉循环）并清理
            pool = getattr(app.state, "worker_pool", None)
            if pool is not None:
                await pool.stop()  # 停全部 worker 循环（并发闸随池回收）
            poll_task = getattr(app.state, "poll_run_task", None)
            if poll_task is not None:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
            if http is not None:
                await http.aclose()
            await engine.dispose()

    docs_disabled = not settings.openapi_enabled
    app = FastAPI(
        title="codereview-ai",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if docs_disabled else "/docs",
        redoc_url=None if docs_disabled else "/redoc",
        openapi_url=None if docs_disabled else "/openapi.json",
    )
    app.add_middleware(TraceMiddleware)
    app.include_router(health_router)
    mount_admin(app, settings.frontend_dist)
    app.include_router(auth_router, prefix="/api")
    app.include_router(projects.router, prefix="/api")
    app.include_router(models.router, prefix="/api")
    app.include_router(members, prefix="/api")
    app.include_router(notifiers.router, prefix="/api")
    app.include_router(forges.router, prefix="/api")
    app.include_router(reviews.router, prefix="/api")
    app.include_router(pull.router, prefix="/api")
    app.include_router(pull.tenant_router, prefix="/api")
    app.include_router(schedules.router, prefix="/api")
    app.include_router(clone_cache.router, prefix="/api")
    app.include_router(admin_settings.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(usage.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(roles.router, prefix="/api")
    app.include_router(webhook_router)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    global _app
    if name == "app":
        # 惰性构建：uvicorn 用 getattr(module, "app") 时触发；import 本模块不触发，
        # 避免测试 import main 时因缺密钥 fail-fast
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=5001)

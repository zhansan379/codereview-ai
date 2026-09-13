"""FastAPI 入口（DESIGN §1）。

- 生命周期：结构化日志 → 建 engine（SQLite 加固）→ 建表 → 暴露到 `app.state` 供 /ready 探测。
- 挂 trace 中间件与健康路由；OpenAPI 文档可由 `CR_OPENAPI_ENABLED=0` 关闭。
- 顶部 `app = create_app()` 在 import 时即构造 `Settings`，缺密钥会 fail-fast 退出。
- webhook 路由挂载后，把 simple 档的 enqueuer 注入 `app.state`；worker 装配走
  ops/bootstrap：条件齐备即启动，不齐备则留下 None，待保存模型/平台凭据或手动执行时
  懒启动（`ensure_worker_started`），全程无需重启。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from codereview_ai.api.admin import (
    clone_cache,
    forges,
    models,
    notifications,
    notifiers,
    projects,
    pull,
    reviews,
    roles,
    schedules,
    stats,
    tasks,
    users,
    workrate,
)
from codereview_ai.api.admin import (
    settings as admin_settings,  # 全局运行时设置（并发数）
)
from codereview_ai.api.admin.notifier_members import router as members  # 系统级 @成员名单
from codereview_ai.api.admin_ui import mount_admin
from codereview_ai.api.auth import router as auth_router
from codereview_ai.api.webhook import WebhookHelpMiddleware
from codereview_ai.api.webhook import router as webhook_router
from codereview_ai.config import Settings
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.logging import setup_logging
from codereview_ai.ops.bootstrap import ensure_worker_started, get_or_create_poller, worker_can_run
from codereview_ai.ops.clone_cache import CloneCachePruner
from codereview_ai.ops.health import router as health_router
from codereview_ai.ops.tracing import TraceMiddleware
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.storage.db import (
    create_engine,
    init_db,
    raise_for_connect_failure,
    session_factory,
)
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.storage.seed import (
    prune_obsolete_permissions,
    seed_rbac,
    sync_permission_catalog,
)
from codereview_ai.worker import (
    EventStore,
    QueueEnqueuer,
    scribble_queued_task,
)

logger = logging.getLogger("codereview_ai.main")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(log_level=settings.log_level)
        engine = create_engine(settings.database_url)
        try:
            await init_db(engine)
        except Exception as exc:
            # 连接层失败（拒绝/超时/DNS/认证）→ 兜底提示 + 干净退出，不甩 traceback；
            # 其他数据库异常原样上抛，保留完整异常栈便于定位。
            raise_for_connect_failure(settings.database_url, exc)
            raise
        app.state.engine = engine
        app.state.settings = settings

        # —— agent 本地克隆缓存：注册表 + 清除策略后台（独立于 agentic 开关，列表/清理恒可用）——
        cache_root = settings.agent_clone_cache_dir or str(
            Path(tempfile.gettempdir()) / "codereview-agent-repos"
        )
        app.state.cache_root = cache_root

        pruner = CloneCachePruner(engine, cache_root)
        await pruner.start()
        app.state.pruner = pruner

        # —— RBAC 种子（F5.11）：MLP 就绪后播种权限目录 + 内置角色 + 首个 admin ——
        async with session_factory(engine)() as s:
            # 对账权限目录↔permission 表：新增码补行、删掉码清残留（均为幂等 no-op 兜底）
            await sync_permission_catalog(s)
            await prune_obsolete_permissions(s)
            await seed_rbac(s, settings.admin_password)

        # —— simple 档队列：webhook 入队即返回 202，worker 异步消费 ——
        store = EventStore()
        queue = AsyncioTaskQueue()
        app.state.queue = queue
        app.state.event_store = store
        app.state.enqueuer = QueueEnqueuer(queue, store)
        http: httpx.AsyncClient | None = None

        # —— DB 驱动配置（DESIGN §16）：LLM 模型/api_key 从 DB 解析，env 重放压 DB
        provider_repo = ConfigRepository(engine, encryption_key=settings.encryption_key)
        app.state.config_repository = provider_repo
        # —— 平台适配器：DB/env 解析 + 保存后热更（ForgeRegistry）——
        http = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        app.state.http = http
        forge_registry = ForgeRegistry(provider_repo, http)
        await forge_registry.refresh_all()
        app.state.forge_registry = forge_registry
        # 「有无可用 worker」不再启动时定格：can_run 动态判 worker_pool 是否存在——
        # worker 懒启动拉起的瞬间，webhook/补拉自动恢复正常入队（无需重启）。
        app.state.enqueuer.can_run = worker_can_run(app)
        # 补拉功能不依赖 LLM，只要 forge 可用就可以创建（can_run 同动态判据）
        get_or_create_poller(app)
        # —— 入队即建 mr 审计行（DESIGN §9.2）：让队列里等待的 PR 从入队起就可见为
        #    『排队中』，而非开审才建行（长任务排队时不可见）。幂等，已审同 head 短路。
        #    无论是否有 LLM 模型都设置，让任务在页面上可见。
        review_repo_for_enqueue = ReviewRepository(engine)

        async def _on_enqueue(provider: str, raw: bytes, *, hold: bool = False) -> None:
            await scribble_queued_task(
                review_repo_for_enqueue, forge_registry.get(provider), raw, hold=hold,
            )

        app.state.enqueuer.on_enqueue = _on_enqueue

        # —— worker 装配：条件齐备即启动（与旧一次性装配等价）；不齐备留下 None，
        #    之后保存模型/平台凭据或手动执行时懒启动，不再要求重启服务 ——
        await ensure_worker_started(app)
        if not getattr(app.state, "worker_pool", None):
            logger.warning(
                "未配置可用 LLM 模型（env CR_LLM_MODEL / DB model_config）或可用平台"
                "（env CR_GITHUB_TOKEN/CR_GITLAB_TOKEN 或设置页 DB），webhook 仍可入队但无 worker"
            )
            # 遗留的 queued/running 行没有消费者，永卡排队无意义 → 批量落「未开始」，
            # 条件就绪（配置 LLM 后保存即懒启动）由回放续跑或用户在审查记录页手动执行。
            n_hold = await review_repo_for_enqueue.mark_pending_not_started(
                reason="no_llm",
                error="启动时未配置可用 LLM（或平台适配器），服务未启动审查 worker，任务未开始",
            )
            if n_hold:
                logger.warning("已把 %s 条遗留排队任务转为「未开始」（no_llm）", n_hold)

        app_port = os.environ.get("CR_APP_PORT", "5001")
        logger.info("管理后台已就绪：http://127.0.0.1:%s/admin", app_port)

        try:
            yield
        finally:
            local_pruner = getattr(app.state, "pruner", None)
            if local_pruner is not None:
                await local_pruner.stop()  # 停清除策略后台循环
            scheduler = getattr(app.state, "scheduler", None)
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
    # 兜底提示中间件：webhook 路径配置错误时返回友好页面
    app.add_middleware(WebhookHelpMiddleware)
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
    app.include_router(schedules.router, prefix="/api")
    app.include_router(clone_cache.router, prefix="/api")
    app.include_router(admin_settings.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(workrate.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    app.include_router(roles.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(notifications.sse_router, prefix="/api")
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

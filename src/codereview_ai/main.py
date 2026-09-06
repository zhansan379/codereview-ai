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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from pathlib import Path

import httpx
from fastapi import FastAPI

from codereview_ai.api.admin import forges, models, notifiers, projects, reviews, stats, tasks
from codereview_ai.api.admin_ui import mount_admin
from codereview_ai.api.auth import router as auth_router
from codereview_ai.api.webhook import router as webhook_router
from codereview_ai.config import Settings
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.logging import setup_logging
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.ops.health import router as health_router
from codereview_ai.ops.periodic import DailyReporter
from codereview_ai.ops.tracing import TraceMiddleware
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.worker import run_worker
from codereview_ai.review.static_analysis import StaticAnalyzer
from codereview_ai.storage.db import create_engine, init_db
from codereview_ai.storage.project_repo import ProjectRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import EventStore, PushGate, QueueEnqueuer, make_processor

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

        # —— simple 档队列：webhook 入队即返回 202，worker 异步消费 ——
        store = EventStore()
        queue = AsyncioTaskQueue()
        app.state.queue = queue
        app.state.enqueuer = QueueEnqueuer(queue, store)
        worker_task: asyncio.Task[None] | None = None
        daily_task: asyncio.Task[None] | None = None
        stop_daily = asyncio.Event()
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
            # F4/M4.7：路由从 DB notifier_config 拉取（project_id=None→仅全局默认）
            notifier = NotifierDispatcher(provider_repo.notifier_routes, http=http)
            # §7.7：push 轨默认关；由 env 开关 + 分支 glob 构造 PushGate
            push_gate = PushGate(
                enabled=settings.push_review_enabled,
                branch_match=_branch_glob_match(settings.push_branch_globs),
            )
            # §11 静态分析：默认开，缺工具自动降级，不影响主链
            ws = Path(settings.static_workspace_dir) if settings.static_workspace_dir else None
            static_analyzer = StaticAnalyzer(enabled=settings.review_static_enabled, workspace=ws)
            # 项目级配置（文件扩展名过滤）：按 (provider, repo_id) 实时读 project 启用行
            project_repo = ProjectRepository(engine)
            processor = make_processor(
                lambda p: forge_registry.get(p), lambda _: reviewer, store, review_repo=review_repo,
                notifier=notifier, push_gate=push_gate, static_analyzer=static_analyzer,
                project_config_factory=project_repo.config_for,
            )
            worker_task = asyncio.create_task(run_worker(queue, processor))
            # M5.7 日报调度：随 worker 生命周期启动/清理（hour 由 CR_DAILY_HOUR 配置）
            reporter = DailyReporter(
                engine, notifier,
                hour=settings.daily_report_hour,
                enabled=settings.daily_report_enabled,
            )
            daily_task = asyncio.create_task(reporter.run_forever(stop_daily))
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

        try:
            yield
        finally:
            stop_daily.set()  # 先唤醒日报循环退出，再取消任务
            for t in (daily_task, worker_task):
                if t is not None:
                    t.cancel()
                    try:
                        await t
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
    app.include_router(notifiers.router, prefix="/api")
    app.include_router(forges.router, prefix="/api")
    app.include_router(reviews.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
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

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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from codereview_ai.api.webhook import router as webhook_router
from codereview_ai.config import Settings
from codereview_ai.forges.registry import build_adapter, registered_providers
from codereview_ai.logging import setup_logging
from codereview_ai.ops.health import router as health_router
from codereview_ai.ops.tracing import TraceMiddleware
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.worker import run_worker
from codereview_ai.review.llm_gateway import LLMGateway
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import create_engine, init_db
from codereview_ai.worker import EventStore, QueueEnqueuer, make_processor

logger = logging.getLogger("codereview_ai.main")


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
        http: httpx.AsyncClient | None = None

        providers = registered_providers(settings)
        if providers and settings.llm_model:
            http = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
            gateway = LLMGateway(model=settings.llm_model)
            reviewer = Reviewer(gateway)
            adapters = {p: build_adapter(p, settings, http) for p in providers}
            processor = make_processor(lambda p: adapters.get(p), lambda _: reviewer, store)
            worker_task = asyncio.create_task(run_worker(queue, processor))
            logger.info("内置 worker 已启动：%s（model %s）", ",".join(providers), settings.llm_model)  # noqa: E501
        else:
            logger.warning(
                "未配置平台 token（CR_GITLAB_TOKEN/CR_GITHUB_TOKEN）或 CR_LLM_MODEL，"
                "webhook 仍可入队但无 worker 消费"
            )

        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                try:
                    await worker_task
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

"""进程内 asyncio 队列（simple 档，DESIGN §8.2/§9）。

单进程自带的 worker 用 `asyncio.Queue`；任务元数据存内存 dict。
重启即清空——simple 档的定位就是"单容器免外部依赖、够本地跑"，不承诺持久化；
持久化/多 worker 交给 standard 档的 arq（Redis）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from codereview_ai.queue.base import TaskMeta, TaskQueue, TaskState


class AsyncioTaskQueue(TaskQueue):
    """内存实现：`asyncio.Queue` 承载待跑 task_id，dict 维护状态。"""

    def __init__(self) -> None:
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: dict[str, TaskMeta] = {}
        self._seq = 0

    def _new_task_id(self) -> str:
        self._seq += 1
        return f"t-{self._seq}"

    async def enqueue(self, provider: str) -> TaskMeta:
        meta = TaskMeta(task_id=self._new_task_id(), provider=provider)
        self._tasks[meta.task_id] = meta
        await self._pending.put(meta.task_id)
        return meta

    async def claim(self) -> TaskMeta | None:
        # 非阻塞 peek：空队列立即返回 None，不阻塞 worker 退出。
        if self._pending.empty():
            return None
        task_id = await self._pending.get()
        meta = self._tasks.get(task_id)
        if meta is None or meta.state is not TaskState.QUEUED:
            return None
        meta.state = TaskState.RUNNING
        meta.started_at = datetime.now(UTC)
        meta.attempts += 1
        return meta

    async def complete(self, task_id: str) -> None:
        meta = self._tasks.get(task_id)
        if meta is None:
            return
        meta.state = TaskState.SUCCEEDED

    async def fail(self, task_id: str) -> None:
        meta = self._tasks.get(task_id)
        if meta is None:
            return
        meta.state = TaskState.FAILED

    async def recover_stale(self, stale_after: float = 300) -> int:
        now = datetime.now(UTC)
        recovered = 0
        for meta in self._tasks.values():
            started = meta.started_at
            if meta.state is TaskState.RUNNING and started is not None:
                age = (now - started).total_seconds()
                if age > stale_after:
                    meta.state = TaskState.QUEUED
                    meta.started_at = None
                    await self._pending.put(meta.task_id)
                    recovered += 1
        return recovered

    def task(self, task_id: str) -> TaskMeta | None:
        """供测试/调试读取任务状态。"""
        return self._tasks.get(task_id)

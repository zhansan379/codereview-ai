"""队列抽象 + 任务状态机（DESIGN §9）。

队列只应有 `enqueue/claim/complete` 三个原子操作，状态机：
`queued → running → succeeded | failed`；`running` 超时被 `recover_stale` 回收回 `queued`。
- 队列**只搬 task_id**，不搬 payload —— worker 从 DB 取任务，天然幂等、重启不丢。
- simple（asyncio）档是进程内队列；standard（arq/Redis）档另实现同接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

#: 运行超时回收阈值（秒）：running 超过此阈值未被标记完成的即视为崩溃，回收重投。
STALE_RUNNING_SECONDS = 300


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class TaskMeta:
    task_id: str
    provider: str
    state: TaskState = TaskState.QUEUED
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None


class TaskQueue(ABC):
    """任务队列抽象。实现者保证状态机合法迁移。"""

    @abstractmethod
    async def enqueue(self, provider: str) -> TaskMeta:
        """入队一个新任务，返回带 task_id 的 TaskMeta。"""

    @abstractmethod
    async def claim(self) -> TaskMeta | None:
        """抢占一个 queued 任务 → running；队列空返回 None。"""

    @abstractmethod
    async def complete(self, task_id: str) -> None:
        """标记成功（succeeded）。"""

    @abstractmethod
    async def fail(self, task_id: str) -> None:
        """标记失败（failed）。"""

    @abstractmethod
    async def recover_stale(self, stale_after: float = STALE_RUNNING_SECONDS) -> int:
        """把 running 超时的任务回收回 queued，返回回收数量。"""

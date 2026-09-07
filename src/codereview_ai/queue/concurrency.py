"""并发闸 + worker 池（DESIGN 并发设置：单 worker 循环 → 可热更并发）。

原 worker 是单个 `run_worker` 协程串行消费队列。这里把「并发数」实现为**可动态扩容的闸门**
`ConcurrencyGate` + 固定数量的 worker 循环 `WorkerPool`：

- 并发上限 = 闸门大小。`async with gate` 借一把额度才处理一条任务，处理完归还；同一时刻最多
  `limit` 个 worker 同时进处理区。
- resize 只改一个计数（`_limit`），在**任务边界**生效，永不 cancel 在跑任务——调低并发不会
  打断正在审查的那几条，只会让后续等额度的 worker 少进来。
- worker 循环数固定为上限常量（`WORKER_LOOP_CAP`），真正限制并行的是闸门，故升并发即时生效
  （有闲置循环可立刻抢额度），降并发安全（不打断）。

AsyncioTaskQueue.claim 对多消费者安全（asyncio.Queue 同 loop 内并发取），故多个 worker 循环
消费同一个队列是安全的；同 head 去重仍由 worker 内 per-head 锁负责（只串行同 head，不同 PR 并行）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from codereview_ai.queue.worker import run_worker

logger = logging.getLogger("codereview_ai.queue.concurrency")

#: worker 循环固定数 = 并发上限。闸门真正限制并行，故这里取到允许的最大值即可。
WORKER_LOOP_CAP = 32


class ConcurrencyGate:
    """可动态调大/调小的并发上限（Event 计数闸，不取消在跑任务）。

    计数：`_inflight`（当前占用额度数）+ `_limit`（上限）。`acquire` 只在 `_inflight >= _limit`
    时挂起等待唤醒；`release` 归还额度并唤醒等待者；`resize` 即时改 `_limit` 并唤醒。

    lost-wakeup 分析：协程只会在 `_inflight == _limit`（确有缺口）时才进入等待，而额度**唯一**
    释放路径是 `release()`（总执行 `_wake.set()`），故不会丢唤醒。已占用的额度在 `resize` 调低
    时不回收——在跑审查不被打断，由后续等待者按新上限重判。
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._inflight = 0
        self._wake = asyncio.Event()

    async def acquire(self) -> None:
        while self._inflight >= self._limit:
            self._wake.clear()
            await self._wake.wait()
        self._inflight += 1

    def release(self) -> None:
        self._inflight -= 1
        self._wake.set()

    def resize(self, limit: int) -> None:
        """把并发上限改为 `limit`（clamp 到 >=1），即时生效；在跑任务不受影响。"""
        self._limit = max(1, int(limit))
        self._wake.set()

    @property
    def limit(self) -> int:
        return self._limit

    # —— async with 支持：进入块 = acquire，退出块 = release ——
    async def __aenter__(self) -> ConcurrencyGate:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False


class WorkerPool:
    """固定数量 worker 循环 + 共享并发闸；resize 即时热更并发，stop 优雅清理。"""

    def __init__(
        self,
        queue: object,  # TaskQueue：run_worker 只用到其 claim/complete/fail/recover_stale
        process: Any,
        *,
        loop_count: int = WORKER_LOOP_CAP,
        limit: int = 4,
    ) -> None:
        self._gate = ConcurrencyGate(limit)
        self._loop_count = max(1, int(loop_count))
        # 门控的 worker 循环：每循环 `async with gate` 处理一条；多个循环并行消费同一队列。
        self._tasks = [
            asyncio.create_task(
                run_worker(queue, process, gate=self._gate, idle_sleep=0.05)
            )
            for _ in range(self._loop_count)
        ]

    def resize(self, limit: int) -> None:
        """热更并发上限（升/降均即时；降不打断在跑审查）。"""
        self._gate.resize(limit)

    @property
    def limit(self) -> int:
        return self._gate.limit

    async def stop(self) -> None:
        """取消全部 worker 循环并等待收尾；幂等。"""
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

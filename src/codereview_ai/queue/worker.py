"""Worker 主循环：抢占 → 处理 → 标记结果（DESIGN §9）。

worker 不关心 payload 内容，只调用注入的 `process(task)` 拿到"审查+回写"动作。
- process 抛异常 → 任务标 failed（不回写评论）；正常返回 → succeeded。
- `run_worker` 是常驻协程；测试用 `max_iterations` 跑有限轮次。
- 空转时周期性 `queue.recover_stale()` 回收 running 超时任务（崩溃恢复）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from codereview_ai.queue.base import TaskMeta, TaskQueue

logger = logging.getLogger("codereview_ai.worker")

#: 处理器：给定 task 执行完整审查动作；异常视为失败。
Processor = Callable[[TaskMeta], Awaitable[None]]


async def run_worker(
    queue: TaskQueue,
    process: Processor,
    *,
    idle_sleep: float = 0.05,
    max_iterations: int | None = None,
    recover_interval: float = 30.0,
) -> None:
    """抢占任务并处理；队列空时短暂休眠。

    `max_iterations` 限定**循环轮数**（含空转），供测试/冒烟终止常驻循环；
    None 表示常驻。空转时周期性 `recover_stale()` 回收 running 超时任务。
    """
    last_recover = monotonic()
    while max_iterations is None or max_iterations > 0:
        if max_iterations is not None:
            max_iterations -= 1
        task = await queue.claim()
        if task is None:
            # 定期回收 running 超时任务（崩溃恢复），避免每次空转都扫表。
            if monotonic() - last_recover >= recover_interval:
                await queue.recover_stale()
                last_recover = monotonic()
            await asyncio.sleep(idle_sleep)
            continue
        try:
            await process(task)
        except Exception:
            logger.exception("task %s failed", task.task_id)
            await queue.fail(task.task_id)
        else:
            await queue.complete(task.task_id)

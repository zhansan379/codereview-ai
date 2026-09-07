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
from typing import TYPE_CHECKING

from codereview_ai.queue.base import TaskMeta, TaskQueue

if TYPE_CHECKING:  # 仅类型标注；运行时避免 concurrency→worker 循环导入
    from codereview_ai.queue.concurrency import ConcurrencyGate

logger = logging.getLogger("codereview_ai.worker")

#: 处理器：给定 task 执行完整审查动作；异常视为失败。
Processor = Callable[[TaskMeta], Awaitable[None]]


async def _run_one(queue: TaskQueue, process: Processor) -> bool:
    """抢占并处理一条：claim → 处理 → complete/fail。返回是否确有活干（空队 False）。

    抽出为独立函数，供 `run_worker` 在（可选）并发闸门内调用——多个 worker 循环并行消费时，
    每条任务的处理逻辑对并发唯一且串行于自身。
    """
    task = await queue.claim()
    if task is None:
        return False
    try:
        await process(task)
    except Exception:
        logger.exception("task %s failed", task.task_id)
        await queue.fail(task.task_id)
    else:
        await queue.complete(task.task_id)
    return True


async def run_worker(
    queue: TaskQueue,
    process: Processor,
    *,
    idle_sleep: float = 0.05,
    max_iterations: int | None = None,
    recover_interval: float = 30.0,
    gate: ConcurrencyGate | None = None,  # 给则每条任务先借并发额度再处理（限并发上限）
) -> None:
    """抢占任务并处理；队列空时短暂休眠。

    `max_iterations` 限定**循环轮数**（含空转），供测试/冒烟终止常驻循环；
    None 表示常驻。空转时周期性 `recover_stale()` 回收 running 超时任务。

    `gate` 非空时，每条任务在 `_run_one` 外套一层并发闸：同一时刻最多 `gate.limit` 个 worker
    在跑审查，其余在 `async with gate` 处排队等额度（用于并发设置热更，见 queue/concurrency）。
    不传 `gate` 时行为与旧版完全一致（单 worker 串行）。
    """
    last_recover = monotonic()
    while max_iterations is None or max_iterations > 0:
        if max_iterations is not None:
            max_iterations -= 1
        if gate is not None:
            async with gate:
                did_work = await _run_one(queue, process)
        else:
            did_work = await _run_one(queue, process)
        if did_work:
            continue
        # 队列空 → 定期回收 running 超时任务（崩溃恢复），避免每次空转都扫表。
        if monotonic() - last_recover >= recover_interval:
            await queue.recover_stale()
            last_recover = monotonic()
        await asyncio.sleep(idle_sleep)

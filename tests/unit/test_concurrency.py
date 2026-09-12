"""队列并发闸 / worker 池 / 设置存取测试（离线，无网络无外部依赖）。

覆盖三个层面：
- `ConcurrencyGate`：并发上限、resize 升/降、挂起等待队列（`async with` 协议）。
- `run_worker(gate=...)` / `WorkerPool`：门控并发上限（峰值 ≤ limit）、全部任务完成、
  不传 gate 时行为与单 worker 串行一致（回归旧语义）。
- `SettingRepository`：`app_setting` 表读写 / 整数往返 / 非法回落。
"""

from __future__ import annotations

import asyncio

from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.concurrency import ConcurrencyGate, WorkerPool
from codereview_ai.queue.worker import run_worker
from codereview_ai.storage.db import create_engine, init_db
from codereview_ai.storage.setting_repo import SettingRepository


async def _pump(ticks: int = 30) -> None:
    """让出事件循环若干拍，保证新建的协程已跑到挂起点（用于断言它确实阻塞）。"""
    for _ in range(ticks):
        await asyncio.sleep(0)


def _wait_until(cond, timeout: float = 2.0):
    """轮询等待 cond 成立（测试辅助的 busy-wait 是刻意的）。"""
    return asyncio.wait_for(_loop_till(cond), timeout)


async def _loop_till(cond):
    while not cond():  # noqa: ASYNC110 — 测试辅助的刻意轮询，等待条件函数为真
        await asyncio.sleep(0.01)


# ── ConcurrencyGate ────────────────────────────────────────────────────────


async def test_gate_caps_at_limit_then_release():
    gate = ConcurrencyGate(2)
    await gate.acquire()
    await gate.acquire()  # 占满两把额度
    third = asyncio.create_task(gate.acquire())  # 第三把应挂起
    await _pump()
    assert not third.done(), "limit=2 已达，第三个 acquire 应挂起"
    gate.release()  # 归还一把 → 挂起者通过
    await asyncio.wait_for(third, 1)


async def test_gate_resize_up_frees_waiting():
    gate = ConcurrencyGate(1)
    await gate.acquire()
    waiter = asyncio.create_task(gate.acquire())
    await _pump()
    assert not waiter.done()
    gate.resize(2)  # 调高 → 即时释放一条额度，等待者醒来
    await asyncio.wait_for(waiter, 1)
    assert gate.limit == 2


async def test_gate_resize_down_blocks_new_but_keeps_inflight():
    gate = ConcurrencyGate(2)
    await gate.acquire()  # inflight=2
    await gate.acquire()
    gate.resize(1)  # 调低：已占用的不回收（在跑不打断）
    waiter = asyncio.create_task(gate.acquire())
    await _pump()
    assert not waiter.done()  # inflight==limit==1 → 无空位
    gate.release()  # inflight 1==limit 1 → 仍无空位
    await _pump()
    assert not waiter.done()
    gate.release()  # inflight 0 < limit → 通过
    await asyncio.wait_for(waiter, 1)


# ── run_worker / WorkerPool：门控并发 ──────────────────────────────────────


async def test_worker_pool_caps_parallelism_and_completes_all():
    queue = AsyncioTaskQueue()
    for _ in range(4):
        await queue.enqueue("t")

    done: list[str] = []
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def process(task) -> None:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)  # gate 在此 Process 之外 acquire/release → 峰值受 limit 约束
        await asyncio.sleep(0.02)  # 模拟异步耗时审查：让多个 worker 同点停留以测重叠
        async with lock:
            active -= 1
            done.append(task.task_id)

    pool = WorkerPool(queue, process, loop_count=8, limit=2)
    try:
        await _wait_until(lambda: len(done) == 4)
    finally:
        await pool.stop()
    assert {t for t in done} == {f"t-{i}" for i in range(1, 5)}
    assert peak <= 2, f"并发应被 gate 限制在 2 以内，实测峰值 {peak}"


async def test_run_worker_no_gate_is_serial():
    """回归：不传 gate → 单 worker 循环串行处理，峰值恒 1（旧语义不变）。"""
    queue = AsyncioTaskQueue()
    for _ in range(3):
        await queue.enqueue("t")
    peak = 0
    active = 0
    lock = asyncio.Lock()

    async def process(task) -> None:
        nonlocal peak, active
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1

    await run_worker(queue, process, max_iterations=20)
    assert peak == 1  # 单个 run_worker 协程 → 严格串行


# ── SettingRepository ──────────────────────────────────────────────────────


async def test_setting_repo_roundtrip(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await init_db(engine)
    try:
        repo = SettingRepository(engine)
        assert await repo.get("worker_concurrency") is None
        assert await repo.get_int("worker_concurrency", 7) == 7  # 缺省回落
        await repo.set("worker_concurrency", "12")
        assert await repo.get("worker_concurrency") == "12"
        assert await repo.get_int("worker_concurrency", 7) == 12
        await repo.set("worker_concurrency", "12")  # 覆盖幂等
        assert await repo.get_int("worker_concurrency", 7) == 12
        await repo.set("worker_concurrency", "not-a-number")
        assert await repo.get_int("worker_concurrency", 7) == 7  # 非法回落
    finally:
        await engine.dispose()


async def test_setting_repo_get_bool_optional(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await init_db(engine)
    try:
        repo = SettingRepository(engine)
        assert await repo.get_bool_optional("missing") is None  # 缺行 → None
        await repo.set("push_review_default", "1")
        assert await repo.get_bool_optional("push_review_default") is True
        await repo.set("push_review_default", "0")
        assert await repo.get_bool_optional("push_review_default") is False
        await repo.set("push_review_default", "junk")
        assert await repo.get_bool_optional("push_review_default") is None  # 非法回落
    finally:
        await engine.dispose()

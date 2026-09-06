"""queue 测试：enqueue/claim 顺序、状态机迁移、超时回收、worker 主循环。

全程纯内存 asyncio 队列，离线可测；处理器/任务用 fake 注入。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.base import TaskState
from codereview_ai.queue.worker import run_worker

# ── enqueue / claim ─────────────────────────────────────────────────────


async def test_enqueue_returns_queued_meta():
    q = AsyncioTaskQueue()
    meta = await q.enqueue("gitlab")
    assert meta.state is TaskState.QUEUED
    assert meta.provider == "gitlab"
    assert meta.task_id.startswith("t-")


async def test_claim_fifo_and_rejects_non_queued():
    q = AsyncioTaskQueue()
    a = await q.enqueue("gitlab")
    await q.enqueue("github")
    claimed = await q.claim()
    assert claimed is not None and claimed.task_id == a.task_id
    assert claimed.state is TaskState.RUNNING


async def test_claim_empty_returns_none():
    q = AsyncioTaskQueue()
    assert await q.claim() is None


async def test_claim_wont_claim_running_again():
    q = AsyncioTaskQueue()
    await q.enqueue("gitlab")
    await q.claim()
    assert await q.claim() is None  # 已 running，不再可抢


# ── 状态迁移 ────────────────────────────────────────────────────────────


async def test_complete_sets_succeeded():
    q = AsyncioTaskQueue()
    meta = await q.enqueue("gitlab")
    await q.claim()
    await q.complete(meta.task_id)
    assert q.task(meta.task_id).state is TaskState.SUCCEEDED


async def test_fail_sets_failed():
    q = AsyncioTaskQueue()
    meta = await q.enqueue("gitlab")
    await q.claim()
    await q.fail(meta.task_id)
    assert q.task(meta.task_id).state is TaskState.FAILED


# ── 超时回收（崩溃恢复）──────────────────────────────────────────────────


async def test_recover_stale_requeues_overdue_running():
    q = AsyncioTaskQueue()
    meta = await q.enqueue("gitlab")
    await q.claim()
    # 伪造 started_at 超过阈值
    q.task(meta.task_id).started_at = datetime.now(UTC) - timedelta(minutes=10)
    recovered = await q.recover_stale(stale_after=300)
    assert recovered == 1
    assert q.task(meta.task_id).state is TaskState.QUEUED
    # 回收后可从队列再次抢到
    claimed = await q.claim()
    assert claimed is not None and claimed.task_id == meta.task_id


async def test_recover_stale_ignores_fresh_running():
    q = AsyncioTaskQueue()
    meta = await q.enqueue("gitlab")
    await q.claim()  # started_at=now，未超时
    assert await q.recover_stale(stale_after=300) == 0
    assert q.task(meta.task_id).state is TaskState.RUNNING


# ── worker 主循环 ───────────────────────────────────────────────────────


async def test_worker_completes_success_and_marks_failure():
    q = AsyncioTaskQueue()
    ok_meta = await q.enqueue("gitlab")
    fail_meta = await q.enqueue("gitlab")
    seen: list[str] = []

    async def process(task) -> None:
        seen.append(task.task_id)
        if task.task_id == fail_meta.task_id:
            raise RuntimeError("boom")

    await run_worker(q, process, max_iterations=2)
    assert set(seen) == {ok_meta.task_id, fail_meta.task_id}
    assert q.task(ok_meta.task_id).state is TaskState.SUCCEEDED
    assert q.task(fail_meta.task_id).state is TaskState.FAILED


async def test_worker_stops_after_max_iterations():
    q = AsyncioTaskQueue()
    await q.enqueue("gitlab")
    seen: list[str] = []

    async def process(task) -> None:
        seen.append(task.task_id)

    await run_worker(q, process, max_iterations=1)
    assert len(seen) == 1


async def test_worker_handles_empty_queue_without_error():
    """空队列时 worker 空转几轮不崩（max_iterations 终止循环）。"""
    q = AsyncioTaskQueue()
    calls: list[str] = []

    async def process(task) -> None:
        calls.append(task.task_id)

    await run_worker(q, process, max_iterations=3, idle_sleep=0.001)
    assert calls == []  # 空队列：claim 恒 None，处理器从未被调用

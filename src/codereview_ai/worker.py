"""webhook → 队列 → worker 的封装（simple 档，DESIGN §6.3/§9/§15.3）。

- `EventStore`：task_id → (provider, raw_body) 的进程内暂存（队列只搬 task_id，
  payload 由 worker 取出后解析，DESIGN §9.1）。
- `QueueEnqueuer`：匹配 webhook 契约 `async enqueue(provider, raw)`，投队列并入 store。
- `process_raw_event`：原始 payload → 解析 PR → 过滤 action → 补 diff_refs → 拉 diff →
  审查 → 回写。
- 网络与 LLM 只出现在 forge/reviewer 注入里；测试可全程离线。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.queue.base import TaskMeta, TaskQueue
from codereview_ai.review.result_writer import ResultWriter
from codereview_ai.review.reviewer import Reviewer

logger = logging.getLogger("codereview_ai.worker")

#: forge / reviewer 工厂：按 provider 给出对应的审查设施（测试注入 fake）。
ForgeFactory = Callable[[str], ForgeAdapter]
ReviewerFactory = Callable[[str], Reviewer]


class EventStore:
    """task_id → (provider, raw_body) 的进程内暂存。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, bytes]] = {}

    def put(self, task_id: str, provider: str, raw: bytes) -> None:
        self._items[task_id] = (provider, raw)

    def get(self, task_id: str) -> tuple[str, bytes] | None:
        return self._items.get(task_id)

    def drop(self, task_id: str) -> None:
        self._items.pop(task_id, None)


class QueueEnqueuer:
    """webhook 契约 `async enqueue(provider, raw)` → 入队 + 暂存 payload。"""

    def __init__(self, queue: TaskQueue, store: EventStore) -> None:
        self._queue = queue
        self._store = store

    async def enqueue(self, provider: str, raw: bytes) -> str:
        meta = await self._queue.enqueue(provider)
        self._store.put(meta.task_id, provider, raw)
        return meta.task_id


def _event_action(data: dict[str, Any]) -> str:
    """归一事件 action：优先 GitLab 的 object_attributes.action，其次顶层 action。"""
    oa = data.get("object_attributes")
    if isinstance(oa, dict) and oa.get("action"):
        return str(oa["action"])
    return str(data.get("action") or "")


async def process_raw_event(forge: ForgeAdapter, reviewer: Reviewer, raw: bytes) -> None:
    """原始 webhook payload → 审查 + 回写。各阶段失败在此抛出，由 worker 标 failed。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return  # 非 JSON 忽略（签名已验，恶意/畸形 payload 不触发审查）
    if not isinstance(data, dict):
        return
    pr = forge.parse_merge_request(data)
    if pr is None:
        return  # 非 merge_request 事件：任务即完成，无需回写
    if not forge.should_review(_event_action(data)):
        return  # close/merge 等动作不触发审查
    refreshed = await forge.fetch_pull_request(pr)  # 补 diff_refs（行级评论 position 必填）
    diffs = await forge.fetch_files(refreshed)
    result = await reviewer.review(pr=refreshed, commits_text=refreshed.title, diffs=diffs)
    await ResultWriter(forge).write(refreshed, diffs, result)


def make_processor(
    forge_factory: ForgeFactory,
    reviewer_factory: ReviewerFactory,
    store: EventStore,
) -> Callable[[TaskMeta], Awaitable[None]]:
    """由 worker 主循环调用的处理函数：根据 task 取 payload 后走完整管线。"""

    async def process(task: TaskMeta) -> None:
        item = store.get(task.task_id)
        if item is None:
            return  # 无暂存 payload（如直接入队的调试任务），视为已处理
        provider, raw = item
        forge = forge_factory(provider)
        reviewer = reviewer_factory(provider)
        await process_raw_event(forge, reviewer, raw)

    return process

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
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.queue.base import TaskMeta, TaskQueue
from codereview_ai.review.group_review import review_in_groups
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.review.increments import (
    REASON_ALREADY,
    IncrementReference,
    IncrementStore,
    collect_fingerprints,
    decide_from_ref,
    dedup_findings,
)
from codereview_ai.review.result_writer import ResultWriter
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.review_repo import ReviewRepository

logger = logging.getLogger("codereview_ai.worker")

#: forge / reviewer 工厂：按 provider 给出对应的审查设施（测试注入 fake）。
#: forge 可能返回 None（该 provider 未配置适配器），worker 跳过而非报错。
ForgeFactory = Callable[[str], ForgeAdapter | None]
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


async def process_raw_event(
    forge: ForgeAdapter,
    reviewer: Reviewer,
    raw: bytes,
    *,
    increments: IncrementStore | None = None,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    chain_valid: Callable[[str, str], bool] | None = None,
    notifier: NotifierDispatcher | None = None,
) -> None:
    """原始 webhook payload → 审查 + 回写。各阶段失败在此抛出，由 worker 标 failed。

    增量（DESIGN §7.3）落点可来自 `review_repo`（DB 持久，M4 起主用）或进程内
    `increments`（M3 内存档兼容）。`grouper` 给定且改动 ≥ 4 个文件时走语义分组并
    发审查（DESIGN §7.5，见 review.group_review）。`chain_valid(prior_sha, head_sha)`
    校验上次 head 是否仍在本 PR 链上（平台 compare），缺省 `None` → 保守回退全量。
    `notifier`（DESIGN F4）给定时，审查+回写成功后后台推送 IM 通知（失败不影响主链）。
    """
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

    # 取上次成功审查落点：DB 仓储优先，其次内存 store；都没有 → 全量
    ref: IncrementReference | None = None
    if review_repo is not None:
        ref = await review_repo.last_ok_review(pr.provider, pr.repo_id, pr.pr_number)
    elif increments is not None:
        ref = increments.last(pr.provider, pr.pr_number)

    valid = chain_valid(ref.head_sha, pr.head_sha) if chain_valid and ref else False
    decision = decide_from_ref(ref, pr, chain_valid=valid)
    if decision.reason == REASON_ALREADY:
        return  # 同一 commit 重放：已审过，跳过
    incremental = decision.is_incremental

    refreshed = await forge.fetch_pull_request(pr)  # 补 diff_refs（行级评论 position 必填）
    diffs = await forge.fetch_files(refreshed)
    if grouper is not None:
        result = await review_in_groups(
            reviewer, grouper, pr=refreshed, commits_text=refreshed.title, diffs=diffs
        )
    else:
        result = await reviewer.review(pr=refreshed, commits_text=refreshed.title, diffs=diffs)

    if incremental:
        # 只审增量：按内容指纹滤掉上次已报过的 finding（DESIGN §7.3 / §13.3）
        result.findings = dedup_findings(result.findings, ref)

    await ResultWriter(forge).write(refreshed, diffs, result)

    if notifier is not None:
        # F4.4 fire-and-forget：推送后台化，不拖慢也不阻断审查主链
        notifier.launch(refreshed, result)

    if increments is not None and refreshed.head_sha:
        # 内存档才显式记录落点；DB 档 findigs 已落 review_finding，由 review_repo 读取
        increments.record(
            pr.provider, pr.pr_number, refreshed.head_sha, collect_fingerprints(result.findings)
        )


def make_processor(
    forge_factory: ForgeFactory,
    reviewer_factory: ReviewerFactory,
    store: EventStore,
    *,
    increments: IncrementStore | None = None,
    review_repo: ReviewRepository | None = None,
    grouper: SemanticGrouper | None = None,
    chain_valid: Callable[[str, str], bool] | None = None,
    notifier: NotifierDispatcher | None = None,
) -> Callable[[TaskMeta], Awaitable[None]]:
    """由 worker 主循环调用的处理函数：根据 task 取 payload 后走完整管线。

    `increments`/`review_repo`/`grouper`/`chain_valid`/`notifier` 透传给
    `process_raw_event`（都缺省时禁用增量/分组/推送，见其 docstring）。
    """

    async def process(task: TaskMeta) -> None:
        item = store.get(task.task_id)
        if item is None:
            return  # 无暂存 payload（如直接入队的调试任务），视为已处理
        provider, raw = item
        forge = forge_factory(provider)
        reviewer = reviewer_factory(provider)
        if forge is None:
            logger.warning("provider %s 未配置适配器，任务 %s 跳过", provider, task.task_id)
            return
        await process_raw_event(
            forge,
            reviewer,
            raw,
            increments=increments,
            review_repo=review_repo,
            grouper=grouper,
            chain_valid=chain_valid,
            notifier=notifier,
        )

    return process

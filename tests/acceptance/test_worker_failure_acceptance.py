"""M6 验收套件 · LLM 失败路径（PRD 标准 6）。

未配置正确 api_key → 审查抛 `LLMError` → 任务标记 failed、MR 上不出现任何评论。
全程离线：webhook → 队列 → worker 全链路，reviewer 注入抛 LLMError 的 fake。

注：MR 轨 `ensure_task` 前置落库（worker.process_raw_event 开头），LLM 失败时任务
落成 `failed` 行（后台可见、可重试）；队列侧同样标 TaskState.FAILED，且 MR 无评论。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from codereview_ai.api.webhook import router
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.base import TaskState
from codereview_ai.queue.worker import run_worker
from codereview_ai.review.llm_gateway import LLMError
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import EventStore, QueueEnqueuer, make_processor

SECRET = "wh-secret"


class _Forge(ForgeAdapter):
    name = "gitlab"

    def __init__(self) -> None:
        self.posted_inline: list[list[dict]] = []
        self.posted_summary: list[str] = []

    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
        from codereview_ai.domain.models import PullRequest

        return PullRequest(
            provider="gitlab", repo_id="4", repo_full_name="acme/z", web_url="",
            pr_number=int(oa["iid"]), title="t", source_branch="s", target_branch="t",
            head_sha="h", base_sha="", diff_refs=None,
        )

    @staticmethod
    def should_review(action: str) -> bool:
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        return pr

    async def fetch_files(self, pr):
        from codereview_ai.domain.models import ChangeType, FileDiff

        return [FileDiff(old_path="a.py", new_path="a.py", diff="---\n+++\n@@ -0,0 +1,1 @@\n+x\n",
                         additions=1, deletions=0, change_type=ChangeType.NEW_FILE)]

    async def post_inline(self, pr, comments: list[dict]) -> None:
        self.posted_inline.append(comments)

    async def post_summary(self, pr, body: str) -> None:
        self.posted_summary.append(body)


class _BadKeyReviewer:
    """模拟 LLM api_key 配错：review 直接抛 LLMError。"""

    async def review(self, *, pr, commits_text, diffs):
        raise LLMError("AuthenticationError: invalid api_key")


def _mr_payload() -> bytes:
    return json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 4,
                              "last_commit": {"id": "h"}},
    }).encode()


async def test_c6_bad_api_key_marks_failed_and_no_comments(tmp_path):
    queue = AsyncioTaskQueue()
    store = EventStore()
    enq = QueueEnqueuer(queue, store)

    app = FastAPI()
    app.include_router(router)
    app.state.settings = type("S", (), {"webhook_secret": SECRET})()
    app.state.enqueuer = enq

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fail.db'}")
    await init_db(engine)
    try:
        repo = ReviewRepository(engine)
        forge = _Forge()
        processor = make_processor(
            lambda p: forge, lambda p: _BadKeyReviewer(), store, review_repo=repo,
        )

        tc = TestClient(app)
        assert tc.post("/webhook", content=_mr_payload(),
                       headers={"X-Gitlab-Token": SECRET}).status_code == 202

        task_id = next(iter(store._items))
        await run_worker(queue, processor, max_iterations=2, idle_sleep=0.001)

        # 任务标记 failed（PRD 标准 6 之"任务标记 failed"）
        assert queue.task(task_id).state is TaskState.FAILED
        # MR 上不出现任何评论（行级 + 总结均为空）
        assert forge.posted_inline == []
        assert forge.posted_summary == []

        # 无 completed 审计行（审查未成功，绝不落 completed）；但失败必须可见（failed 行）
        session = session_factory(engine)
        async with session() as s:
            completed = int((await s.execute(
                select(func.count()).select_from(ReviewTask).where(
                    ReviewTask.state == "completed",
                )
            )).scalar_one() or 0)
            failed = int((await s.execute(
                select(func.count()).select_from(ReviewTask).where(
                    ReviewTask.state == "failed",
                )
            )).scalar_one() or 0)
        assert completed == 0
        assert failed == 1  # ensure_task 前置落库 + except 标 failed → 静默丢任务已消除
    finally:
        await engine.dispose()

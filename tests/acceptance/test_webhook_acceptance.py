"""M6 验收套件 · webhook 侧（PRD 标准 2 / 3 / 7）。

- 标准 2：伪造签名 → 401 且不入队（GitLab 明文 token / GitHub HMAC 两条路径）。
- 标准 3：同一 MR 重复推送 5 次 → 只审查 1 次（DB 幂等 + 增量 ALREADY 跳过）。
- 标准 7：审查中途"崩溃"（任务滞留 RUNNING）→ 重启后 recover_stale 回收续跑最终完成。

全程离线：httpx/TestClient + 临时 SQLite + fake forge/reviewer。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from codereview_ai.api.webhook import router
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.base import TaskState
from codereview_ai.queue.worker import run_worker
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import EventStore, QueueEnqueuer, make_processor

SECRET = "wh-secret"


# ── 复用 worker 管线测试的 fake forge / reviewer（头可参数化）──────────────


class _FakeForge(ForgeAdapter):
    name = "gitlab"

    def __init__(self, head: str = "h") -> None:
        self.head = head
        self.posted_inline: list[list[dict]] = []
        self.posted_summary: list[str] = []

    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
        from codereview_ai.domain.models import PullRequest

        return PullRequest(
            provider="gitlab", repo_id="7", repo_full_name="acme/widgets",
            web_url="", pr_number=int(oa["iid"]), title=str(oa.get("title") or ""),
            source_branch="s", target_branch="t", head_sha=self.head,
            base_sha="", diff_refs=None,
        )

    @staticmethod
    def should_review(action: str) -> bool:
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        return pr

    async def fetch_files(self, pr):
        from codereview_ai.domain.models import ChangeType, FileDiff

        return [FileDiff(old_path="a.py", new_path="a.py", diff="---\n+++\n@@ -0,0 +1,2 @@\n+ctx\n",
                         additions=2, deletions=0, change_type=ChangeType.NEW_FILE)]

    async def post_inline(self, pr, comments: list[dict]) -> None:
        self.posted_inline.append(comments)

    async def post_summary(self, pr, body: str) -> None:
        self.posted_summary.append(body)


class _FakeReviewer:
    def __init__(self, forge: _FakeForge) -> None:
        self.forge = forge
        self.calls: list[dict] = []

    async def review(self, *, pr, commits_text, diffs):
        from codereview_ai.domain.models import (
            Category,
            Finding,
            ReviewResult,
            ReviewScores,
            Severity,
        )

        self.calls.append({"pr": pr.pr_number, "head": pr.head_sha})
        result = ReviewResult(
            summary="评审完毕",
            scores=ReviewScores(correctness=30, security=20, practices=15,
                                performance=4, commit_quality=3),
        )
        result.findings.append(Finding(
            content="新增行问题", category=Category.BUG, severity=Severity.HIGH,
            existing_code="+ctx\n", file="a.py", line=2,
        ))
        return result


def _mr_payload() -> bytes:
    return json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 7, "target_project_id": 7,
                              "title": "add feature", "last_commit": {"id": "h"}},
    }).encode()


# ── 标准 2：伪造签名 → 401 且不产生任务 ────────────────────────────────────


class _FakeEnqueuer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    async def enqueue(self, provider: str, raw: bytes) -> None:
        self.calls.append((provider, raw))


@pytest.fixture()
def _web_app():
    enq = _FakeEnqueuer()
    app = FastAPI()
    app.include_router(router)
    app.state.settings = type("S", (), {"webhook_secret": SECRET})()
    app.state.enqueuer = enq
    return TestClient(app), enq


def test_c2_forged_gitlab_token_401_and_no_enqueue(_web_app):
    tc, enq = _web_app
    r = tc.post("/webhook", content=_mr_payload(), headers={
        "X-Gitlab-Token": "wrong", "Content-Type": "application/json"})
    assert r.status_code == 401
    assert enq.calls == []  # 不产生任何任务


def test_c2_forged_github_hmac_401_and_no_enqueue(_web_app):
    tc, enq = _web_app
    sig = "sha256=" + hmac.new(b"different", _mr_payload(), hashlib.sha256).hexdigest()
    r = tc.post("/webhook", content=_mr_payload(), headers={
        "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig})
    assert r.status_code == 401
    assert enq.calls == []


# ── 标准 3 + 7：真实 webhook → 队列 → worker 全链路 ───────────────────────


async def _wired_harness(tmp_path, head: str = "h"):
    """webhook 用真实队列入队；返回 (tc, queue, processor, forge, reviewer, engine)。"""
    queue = AsyncioTaskQueue()
    store = EventStore()
    enq = QueueEnqueuer(queue, store)

    app = FastAPI()
    app.include_router(router)
    app.state.settings = type("S", (), {"webhook_secret": SECRET})()
    app.state.enqueuer = enq

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'wh.db'}")
    await init_db(engine)
    repo = ReviewRepository(engine)

    forge = _FakeForge(head=head)
    reviewer = _FakeReviewer(forge)  # 共享同一实例 → 跨任务累计 calls

    processor = make_processor(
        lambda p: forge, lambda p: reviewer, store, review_repo=repo,
    )
    return TestClient(app), queue, processor, forge, reviewer, engine


async def _count_rows(engine) -> int:
    session = session_factory(engine)
    async with session() as s:
        return int((await s.execute(
            select(func.count()).select_from(ReviewTask).where(
                ReviewTask.provider == "gitlab", ReviewTask.repo_id == "7",
                ReviewTask.pr_number == 7,
            )
        )).scalar_one() or 0)


async def test_c3_five_identical_webhooks_review_once(tmp_path):
    tc, queue, processor, forge, reviewer, engine = await _wired_harness(tmp_path)
    try:
        # 同一 MR 连续投 5 次（每次都 202 入队）
        for _ in range(5):
            r = tc.post("/webhook", content=_mr_payload(), headers={"X-Gitlab-Token": SECRET})
            assert r.status_code == 202

        await run_worker(queue, processor, max_iterations=20, idle_sleep=0.001)

        assert len(reviewer.calls) == 1  # 只审查 1 次
        assert len(forge.posted_summary) == 1  # 总结评论只回写一条
        assert await _count_rows(engine) == 1  # 幂等：只落 1 条 review_task
    finally:
        await engine.dispose()


async def test_c7_crash_then_restart_recovers_and_completes(tmp_path):
    tc, queue, processor, forge, reviewer, engine = await _wired_harness(tmp_path)
    try:
        assert tc.post("/webhook", content=_mr_payload(),
                       headers={"X-Gitlab-Token": SECRET}).status_code == 202

        # 模拟"审查中途崩溃"：任务被 claim 后停留在 RUNNING、从未 complete
        meta = await queue.claim()
        assert meta is not None
        assert meta.state is TaskState.RUNNING
        # 伪造已滞留超时（> stale_after）
        queue.task(meta.task_id).started_at = datetime.now(UTC) - timedelta(minutes=10)

        # 重启后：回收滞留任务 → 重新排队 → 续跑完成
        assert await queue.recover_stale(stale_after=300) == 1
        assert queue.task(meta.task_id).state is TaskState.QUEUED

        await run_worker(queue, processor, max_iterations=5, idle_sleep=0.001)

        assert reviewer.calls == [{"pr": 7, "head": "h"}]  # 最终完成审查
        assert queue.task(meta.task_id).state is TaskState.SUCCEEDED
    finally:
        await engine.dispose()

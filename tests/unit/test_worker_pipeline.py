"""worker 管线测试：QueueEnqueuer + EventStore + make_processor 端到端。

用 ForgeAdapter 子类 fake 断言「入队 → worker → 审查 → 回写」整条链，
全程离线；LLM/reviewer 也用 fake 注入。
"""

from __future__ import annotations

import json
from typing import Any

from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.worker import run_worker
from codereview_ai.worker import (
    EventStore,
    QueueEnqueuer,
    make_processor,
    process_raw_event,
)


class _FakeForge(ForgeAdapter):
    name = "gitlab"

    def __init__(self) -> None:
        self.posted_inline: list[list[dict]] = []
        self.posted_summary: list[str] = []
        self.diff = "+added\n"

    # ForgeAdapter 抽象实现
    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
        from codereview_ai.domain.models import PullRequest

        return PullRequest(
            provider="gitlab", repo_id=str(oa.get("target_project_id") or 7),
            repo_full_name="acme/widgets", web_url="", pr_number=int(oa["iid"]),
            title=str(oa.get("title") or ""), source_branch="s", target_branch="t",
            head_sha=str(((oa.get("last_commit") or {}) or {}).get("id") or "h"),
            base_sha="", diff_refs=None,
        )

    @staticmethod
    def should_review(action: str) -> bool:
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        from dataclasses import replace

        return replace(pr, diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"})

    async def fetch_files(self, pr):
        from codereview_ai.domain.models import ChangeType, FileDiff

        return [FileDiff(old_path="a.py", new_path="a.py", diff="---\n+++\n@@ -0,0 +1,2 @@\n+ctx\n" + self.diff,  # noqa: E501
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

        self.calls.append({"pr": pr.pr_number, "commits_text": commits_text, "n_diffs": len(diffs)})
        result = ReviewResult(
            summary="评测完毕",
            scores=ReviewScores(correctness=30, security=20, practices=15, performance=4, commit_quality=3),  # noqa: E501
        )
        result.findings.append(Finding(
            content="新增行问题", category=Category.BUG, severity=Severity.HIGH,
            existing_code=self.forge.diff, file="a.py", line=2,
        ))
        return result


def _mr_payload() -> bytes:
    return json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 7, "target_project_id": 7,
                              "title": "add feature", "last_commit": {"id": "h"}},
    }).encode()


# ── process_raw_event 各分支 ────────────────────────────────────────────


async def test_process_skips_non_merge_request():
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    await process_raw_event(forge, reviewer, b'{"object_kind": "push"}')  # type: ignore[arg-type]
    assert reviewer.calls == []


async def test_process_skips_close_action():
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    payload = json.dumps({"object_kind": "merge_request",
                          "object_attributes": {"action": "close", "iid": 7}}).encode()
    await process_raw_event(forge, reviewer, payload)  # type: ignore[arg-type]
    assert reviewer.calls == []


async def test_process_runs_review_and_writes_back():
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    await process_raw_event(forge, reviewer, _mr_payload())  # type: ignore[arg-type]
    assert reviewer.calls and reviewer.calls[0]["pr"] == 7
    assert len(forge.posted_inline) == 1  # 锚定到行 2 → 行级评论
    assert forge.posted_summary  # 总结评论


# ── 端到端：enqueue → worker ────────────────────────────────────────────


async def test_enqueue_worker_review_succeeds():
    store = EventStore()
    queue = AsyncioTaskQueue()
    enqueuer = QueueEnqueuer(queue, store)
    forge = _FakeForge()

    await enqueuer.enqueue("gitlab", _mr_payload())
    task_id = list(store._items)[0]

    processor = make_processor(lambda p: forge, lambda p: _FakeReviewer(forge), store)
    await run_worker(queue, processor, max_iterations=1)

    assert forge.posted_summary  # 回写发生
    assert queue.task(task_id).state.value == "succeeded"

    import codereview_ai.queue.base as base
    assert queue.task(task_id).state is base.TaskState.SUCCEEDED

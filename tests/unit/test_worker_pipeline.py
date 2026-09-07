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
    apply_extension_filter,
    make_processor,
    parse_file_extensions,
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


# ── 增量（DESIGN §7.3）：IncrementStore + chain_valid ───────────────────


class _RecordingReviewRepo:
    """记录 mark_state 状态的 review_repo 桩：验证 worker「开审即标 running」。"""

    def __init__(self) -> None:
        self.states: list[str] = []

    async def last_ok_review(self, *a, **k):
        return None

    async def ensure_task(self, *a, **k):
        return 1

    async def mark_state(self, task_id, *, state, **k):
        self.states.append(state)

    async def insert_findings(self, *a, **k):
        pass

    async def reconcile_findings(self, **k):
        return frozenset()


async def test_process_marks_running_then_completed():
    """mr 轨开审即标 running，收尾翻 completed（供管理页区分「正在跑」）。"""
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    repo = _RecordingReviewRepo()
    await process_raw_event(  # type: ignore[arg-type]
        forge, reviewer, _mr_payload(), review_repo=repo
    )
    assert "running" in repo.states
    assert repo.states[-1] == "completed"


async def test_process_same_head_skips_when_increments_enabled():
    from codereview_ai.review.increments import IncrementStore

    store = IncrementStore()
    store.record("gitlab", 7, "h")  # 上次审过 head=h
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    await process_raw_event(  # type: ignore[arg-type]
        forge, reviewer, _mr_payload(), increments=store, chain_valid=lambda a, b: True
    )
    assert reviewer.calls == []  # 已审过，跳过


async def test_process_incremental_dedups_previous_finding():
    from codereview_ai.domain.models import Category, Finding, Severity
    from codereview_ai.review.increments import IncrementStore, collect_fingerprints

    store = IncrementStore()
    # 上次以 HEAD=h0 审查过，已报到该 finding → 本轮同名内容应被去重
    prior = Finding(content="新增行问题", category=Category.BUG, severity=Severity.HIGH,
                    existing_code="+added\n", file="a.py", line=2)
    store.record("gitlab", 7, "h0", collect_fingerprints([prior]))

    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    # 本轮 fake 解析出的 head 是 "h"（≠h0），chain_valid=True → 增量
    await process_raw_event(  # type: ignore[arg-type]
        forge, reviewer, _mr_payload(), increments=store, chain_valid=lambda a, b: True
    )
    assert reviewer.calls  # 走了一次 review
    # 该 finding 与上次内容指纹相同 → 被 dedup 掉，无行级评论；总结仍回写
    assert forge.posted_inline == []
    assert forge.posted_summary


# ── 文件扩展名过滤（DESIGN：接入 diff 管线）──────────────


def _diff(old: str = "", new: str = ""):
    from codereview_ai.domain.models import ChangeType, FileDiff

    return FileDiff(old_path=old, new_path=new or old, diff="---\n+++\n@@ -1 +1 @@\n",
                    additions=1, deletions=0, change_type=ChangeType.MODIFIED)


def test_parse_file_extensions_normalizes():
    assert parse_file_extensions(".py, .ts\n") == {"py", "ts"}
    assert parse_file_extensions("") == set()
    assert parse_file_extensions("  ,  ") == set()
    assert parse_file_extensions("Py") == {"py"}  # 去前导点 + 小写


def test_apply_extension_filter_empty_keeps_all():
    diffs = [_diff(new="a.py"), _diff(new="a.md"), _diff(new="Makefile")]
    assert apply_extension_filter(diffs, "") == diffs  # 空 → 原样


def test_apply_extension_filter_keeps_only_matching():
    diffs = [_diff(new="a.py"), _diff(new="b.ts"), _diff(new="c.md"), _diff(new="README")]
    kept = apply_extension_filter(diffs, ".py,.ts")
    assert [d.new_path for d in kept] == ["a.py", "b.ts"]


def test_apply_extension_filter_case_insensitive():
    diffs = [_diff(new="Main.PY")]
    assert apply_extension_filter(diffs, ".py") == diffs


def test_apply_extension_filter_deleted_file_uses_old_path():
    diffs = [_diff(old="gone.ts", new="")]  # 删除文件：new_path 空 → 走 old_path
    assert apply_extension_filter(diffs, ".ts") == diffs
    assert apply_extension_filter(diffs, ".py") == []


class _MultiForge:
    """返回多文件的 forge 桩：暴露 fetch 出的 diff 与回写记录。"""

    name = "gitlab"

    def __init__(self, paths: list[tuple[str, str]]) -> None:
        self._paths = paths
        self.fetch_called = 0
        self.posted_summary: list[str] = []

    def parse_merge_request(self, data):
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
    def should_review(action):
        return action in {"open", "opened", "update", "synchronize"}

    async def fetch_pull_request(self, pr):
        from dataclasses import replace
        return replace(pr, diff_refs={"base_sha": "b", "head_sha": "h", "start_sha": "s"})

    async def fetch_files(self, pr):
        self.fetch_called += 1
        return [_diff(old=p, new=n) for p, n in self._paths]

    async def post_inline(self, pr, comments):  # 不产行级，过滤后无 finding
        pass

    async def post_summary(self, pr, body):
        self.posted_summary.append(body)


class _RecordingReviewer:
    """记录实际收到的 diff 路径，供断言过滤生效。"""

    def __init__(self) -> None:
        self.received: list[list[str]] = []

    async def review(self, *, pr, commits_text, diffs):
        self.received.append([d.new_path or d.old_path for d in diffs])
        from codereview_ai.domain.models import ReviewResult, ReviewScores

        return ReviewResult(summary="s", scores=ReviewScores(
            correctness=1, security=1, practices=1, performance=1, commit_quality=1))


async def test_process_filters_diffs_by_project_extensions():
    forge = _MultiForge([("a.py", "a.py"), ("a.md", "a.md"), ("b.ts", "b.ts")])
    reviewer = _RecordingReviewer()

    async def factory(provider, repo_id):
        from codereview_ai.storage.project_repo import ProjectConfig
        return ProjectConfig(file_extensions=".py,.ts")

    await process_raw_event(forge, reviewer, _mr_payload(),  # type: ignore[arg-type]
                            project_config_factory=factory)
    assert reviewer.received == [["a.py", "b.ts"]]  # .md 被过滤


async def test_process_factory_none_keeps_all():
    forge = _MultiForge([("a.py", "a.py"), ("a.md", "a.md")])
    reviewer = _RecordingReviewer()
    await process_raw_event(forge, reviewer, _mr_payload())  # type: ignore[arg-type]
    assert reviewer.received == [["a.py", "a.md"]]  # 未注入 → 不过滤


async def test_process_all_filtered_skips_llm():
    forge = _MultiForge([("a.py", "a.py")])
    reviewer = _RecordingReviewer()

    async def factory(provider, repo_id):
        from codereview_ai.storage.project_repo import ProjectConfig
        return ProjectConfig(file_extensions=".md")  # 全部滤掉

    await process_raw_event(forge, reviewer, _mr_payload(),  # type: ignore[arg-type]
                            project_config_factory=factory)
    assert reviewer.received == []  # 不调 LLM
    assert forge.posted_summary == []  # 不回写


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

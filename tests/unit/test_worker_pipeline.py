"""worker 管线测试：QueueEnqueuer + EventStore + make_processor 端到端。

用 ForgeAdapter 子类 fake 断言「入队 → worker → 审查 → 回写」整条链，
全程离线；LLM/reviewer 也用 fake 注入。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select

from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.queue.asyncio import AsyncioTaskQueue
from codereview_ai.queue.worker import run_worker
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import (
    EventStore,
    QueueEnqueuer,
    apply_extension_filter,
    make_processor,
    parse_file_extensions,
    process_raw_event,
    scribble_queued_task,
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

    async def review(self, *, pr, commits_text, diffs, usage_sink=None):
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
        self.exec_modes: list[str] = []
        self.metrics: list[tuple[int, int, int]] = []

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

    async def set_coverage(self, *a, **k):
        pass  # 覆盖集写入（diff_snapshot，未变更文件复用用）—— 桩不落 DB

    async def set_exec_metrics(self, task_id, *, exec_mode, diff_lines, chat_rounds, tool_calls):
        self.exec_modes.append(exec_mode)
        self.metrics.append((diff_lines, chat_rounds, tool_calls))

    async def last_covered(self, *a, **k):
        return None


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
    # 默认 diff 策略 → exec_mode/diff_lines/chat/tool 随收尾一起落库
    assert repo.exec_modes == ["diff"]
    assert len(repo.metrics) == 1
    assert repo.metrics[0][0] == 2  # _FakeForge MR diff：1 增 + 1 删 = 2 变更行


async def test_process_persists_then_marks_writeback_failed_on_post_error():
    """回写失败（兜底方案 1+6+7）：成果先落库，回写失败打 writeback_failed=True、
    任务保持 completed + 可读 error、发 IM 提示，且**不向外抛**（不触发昂贵重算）。"""
    class _FailWriteForge(_FakeForge):
        async def post_inline(self, pr, comments):
            raise RuntimeError("network boom")

        async def post_summary(self, pr, body):
            raise RuntimeError("network boom")

    class _Repo:
        def __init__(self) -> None:
            self.states: list[tuple[str, dict]] = []
            self.writeback: list[bool] = []

        async def last_ok_review(self, *a, **k):
            return None

        async def ensure_task(self, *a, **k):
            return 1

        async def mark_state(self, task_id, *, state, **k):
            self.states.append((state, k))

        async def insert_findings(self, *a, **k):
            pass

        async def reconcile_findings(self, **k):
            return frozenset()

        async def set_coverage(self, *a, **k):
            pass

        async def set_exec_metrics(self, *a, **k):
            pass  # 执行态四列快照：回写失败路径同样先落 exec_mode（桩不落 DB）

        async def last_covered(self, *a, **k):
            return None

        async def mark_writeback(self, task_id, failed):
            self.writeback.append(failed)

        async def task_project_id(self, task_id):
            return None

    class _Notifier:
        def __init__(self) -> None:
            self.send_markdown_titles: list[str] = []

        def launch(self, pr, result, *, project_id=None):
            return asyncio.create_task(asyncio.sleep(0))

        async def send_markdown(self, title, markdown, *, project_id=None):
            self.send_markdown_titles.append(title)
            return 0

    forge = _FailWriteForge()
    reviewer = _FakeReviewer(forge)
    repo = _Repo()
    notifier = _Notifier()
    # 回写失败被兜住，不向外抛（避免 worker 把整轮标 failed 触发重算）
    await process_raw_event(  # type: ignore[arg-type]
        forge, reviewer, _mr_payload(), review_repo=repo, notifier=notifier,
    )
    assert repo.writeback == [True]  # 打标：回写失败，前端展示「重新发送」
    assert repo.states[-1][0] == "completed"  # 保持 completed，不标 failed 不重算
    assert "回写失败" in repo.states[-1][1].get("error", "")  # 可读提示
    # 人工兜底（fire-and-forget）：让后台 create_task 跑一拍再断言已发出 IM 提示
    await asyncio.sleep(0)
    assert notifier.send_markdown_titles


async def test_process_clears_error_when_writeback_succeeds():
    """回写成功 → 不落 writeback_failed（正常路径无人标记、不丢 completed 文案）。"""
    repo = _RecordingReviewRepo()
    forge = _FakeForge()
    await process_raw_event(  # type: ignore[arg-type]
        forge, _FakeReviewer(forge), _mr_payload(), review_repo=repo,
    )
    assert repo.states[-1] == "completed"
    # _RecordingReviewRepo 不实现 mark_writeback → 说明正常路径根本不调用它
    assert not hasattr(repo, "writeback") or repo.writeback == []


async def test_concurrent_same_head_reviews_only_once(tmp_path):
    """并发同 head 两拨审查 → per-head 锁串行：只有一次 reviewed，另一拨等锁后见
    已完成锚点 → already；评论/总结只写一次（防删记录后重拉的重复审查/重复 IM）。"""
    from codereview_ai.worker import review_pull_request

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    await init_db(engine)
    repo = ReviewRepository(engine)
    forge = _FakeForge()
    reviewer = _FakeReviewer(forge)
    pr = forge.parse_merge_request(json.loads(_mr_payload().decode()))  # head=hh

    a, b = await asyncio.gather(
        review_pull_request(forge, reviewer, pr, review_repo=repo),
        review_pull_request(forge, reviewer, pr, review_repo=repo),
    )
    assert sorted([a, b]) == ["already", "reviewed"]
    assert len(forge.posted_summary) == 1  # 评论只发一次
    assert len(forge.posted_inline) == 1  # 行级评论只发一次


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

    return FileDiff(old_path=old, new_path=new or old, diff="---\n+++\n@@ -1 +1 @@\n+changed\n",
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

    async def review(self, *, pr, commits_text, diffs, usage_sink=None):
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


class _EmptyDiffForge(_MultiForge):
    """返回 diff 列表非空、但实变行数为 0 的文件（只有 hunk 头，无 +/- 变更行）。"""

    async def fetch_files(self, pr):
        self.fetch_called += 1
        from codereview_ai.domain.models import ChangeType, FileDiff
        return [FileDiff(old_path="a.py", new_path="a.py",
                         diff="---\n+++\n@@ -1 +1 @@\n",
                         additions=0, deletions=0, change_type=ChangeType.MODIFIED)]


async def test_process_zero_change_lines_skips_llm():
    """diff 列表非空但实变行数为 0（空/重命名）→ 不调 LLM。

    此前 `if not diffs` 放过此档，空输入把 LLM 逼出空返回落 failed（#23 即此因）。
    """
    forge = _EmptyDiffForge([("a.py", "a.py")])
    reviewer = _RecordingReviewer()
    await process_raw_event(forge, reviewer, _mr_payload())  # type: ignore[arg-type]
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


# ── 入队即建行（scribble，DESIGN §9.2）：等待的 PR 从入队起可见 ──────────────


async def test_enqueue_scribbles_mr_row_visible_before_process(tmp_path):
    """入队即建 mr 审计行（state=queued），审完后翻 completed——「排队中」不再是隐形态。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    await init_db(engine)
    repo = ReviewRepository(engine)
    forge = _FakeForge()
    store = EventStore()
    queue = AsyncioTaskQueue()
    enqueuer = QueueEnqueuer(
        queue, store, on_enqueue=lambda p, r: scribble_queued_task(repo, forge, r)
    )

    await enqueuer.enqueue("gitlab", _mr_payload())

    async with session_factory(engine)() as s:
        row = (await s.execute(select(ReviewTask))).scalar_one()
    assert row.state == "queued"  # 入队即可见，无需等 worker 开审
    assert row.pr_number == 7 and row.payload  # 身份 + 原始 body 已落，供回放/重试

    processor = make_processor(lambda p: forge, lambda p: _FakeReviewer(forge), store,
                               review_repo=repo)
    await run_worker(queue, processor, max_iterations=1)

    async with session_factory(engine)() as s:
        row = (await s.execute(select(ReviewTask))).scalar_one()
    assert row.state == "completed"  # 审完翻终态，未残留多余排队行


async def test_scribble_skips_non_review_action_and_non_json(tmp_path):
    """close/merge 等不审动作不入队列也不建行；非 mr payload 不建行。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    await init_db(engine)
    repo = ReviewRepository(engine)
    forge = _FakeForge()
    enqueuer = QueueEnqueuer(
        AsyncioTaskQueue(), EventStore(),
        on_enqueue=lambda p, r: scribble_queued_task(repo, forge, r),
    )

    close = json.dumps({"object_kind": "merge_request",
                        "object_attributes": {"action": "close", "iid": 7}}).encode()
    await enqueuer.enqueue("gitlab", close)
    await enqueuer.enqueue("gitlab", b'{"object_kind": "push"}')

    async with session_factory(engine)() as s:
        rows = (await s.execute(select(ReviewTask))).scalars().all()
    assert rows == []  # 都不建行（process 同样跳过）


# ── F3.7：评分低于阈值 → 阻塞合并（commit status）──────────────────────────


class _StatusForge(_MultiForge):
    """在 _MultiForge 记录 post_commit_status 调用。"""

    def __init__(self, paths: list[tuple[str, str]]) -> None:
        super().__init__(paths)
        self.statuses: list[tuple[bool, str]] = []

    async def post_commit_status(self, pr, *, passed: bool, description: str = "") -> None:
        self.statuses.append((passed, description))


class _TotalReviewer(_RecordingReviewer):
    """返回指定总分的审查结果（正确性 40 + 安全 30 + 工程 20 + 性能 5 + 提交 5 = 100）。"""

    def __init__(self, total: int) -> None:
        super().__init__()
        self._total = total

    async def review(self, *, pr, commits_text, diffs, usage_sink=None):
        from codereview_ai.domain.models import ReviewResult, ReviewScores

        self.received.append([d.new_path or d.old_path for d in diffs])
        correctness = min(self._total, 40)
        security = min(self._total - correctness, 30)
        practices = min(self._total - correctness - security, 20)
        performance = min(self._total - correctness - security - practices, 5)
        commit_quality = max(self._total - correctness - security - practices - performance, 0)
        return ReviewResult(summary="s", scores=ReviewScores(
            correctness=correctness, security=security, practices=practices,
            performance=performance, commit_quality=commit_quality))


async def test_enforce_score_threshold_below_posts_failed():
    forge = _StatusForge([("a.py", "a.py")])
    reviewer = _TotalReviewer(5)  # 总分 5 < 90

    async def factory(provider, repo_id):
        from codereview_ai.storage.project_repo import ProjectConfig
        return ProjectConfig(enforce_score_threshold=True, score_threshold=90)

    await process_raw_event(forge, reviewer, _mr_payload(),  # type: ignore[arg-type]
                            project_config_factory=factory)
    assert forge.statuses == [(False, "AI 审查 5/100")]


async def test_enforce_score_threshold_reached_posts_success():
    forge = _StatusForge([("a.py", "a.py")])
    reviewer = _TotalReviewer(95)  # 总分 95 ≥ 90

    async def factory(provider, repo_id):
        from codereview_ai.storage.project_repo import ProjectConfig
        return ProjectConfig(enforce_score_threshold=True, score_threshold=90)

    await process_raw_event(forge, reviewer, _mr_payload(),  # type: ignore[arg-type]
                            project_config_factory=factory)
    assert forge.statuses == [(True, "AI 审查 95/100")]


async def test_enforce_score_threshold_disabled_no_status():
    forge = _StatusForge([("a.py", "a.py")])
    reviewer = _TotalReviewer(5)

    async def factory(provider, repo_id):
        from codereview_ai.storage.project_repo import ProjectConfig
        return ProjectConfig(enforce_score_threshold=False, score_threshold=90)

    await process_raw_event(forge, reviewer, _mr_payload(),  # type: ignore[arg-type]
                            project_config_factory=factory)
    assert forge.statuses == []  # 开关关 → 不发状态（回归：默认行为不变）


# ── 补拉入队（enqueue_pr 携带已解析 PullRequest）→ worker 消费 ────────────


async def test_enqueue_pr_worker_consumes_and_reviews(tmp_path):
    """补拉入队的已解析 PR 走 worker 异步审查：消费 → 审查执行一次 → 审计行翻 completed。"""
    from codereview_ai.domain.models import PullRequest

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'poll.db'}")
    await init_db(engine)
    repo = ReviewRepository(engine)
    forge = _FakeForge()
    store = EventStore()
    queue = AsyncioTaskQueue()
    enqueuer = QueueEnqueuer(queue, store)

    pr = PullRequest(
        provider="gitlab", repo_id="7", repo_full_name="acme/widgets",
        web_url="https://x/7", pr_number=999, title="poller enqueued",
        source_branch="s", target_branch="t", head_sha="h-poll-999", base_sha="b",
    )
    # 补拉侧本就在入队前落了 queued 行——这里模拟该行已存在（ensure_task 幂等复用）。
    await repo.ensure_task(provider="gitlab", repo_id="7", pr_number=999,
                           event_type="mr", branch="s", head_sha="h-poll-999",
                           base_sha="b", pr_title="poller enqueued", payload="")
    await enqueuer.enqueue_pr("gitlab", pr)

    processor = make_processor(lambda p: forge, lambda p: _FakeReviewer(forge), store,
                               review_repo=repo)
    await run_worker(queue, processor, max_iterations=1)

    assert forge.posted_summary  # 审查执行并回写总结
    async with session_factory(engine)() as s:
        row = (await s.execute(select(ReviewTask))).scalar_one()
    assert row.state == "completed" and row.pr_number == 999


def test_count_diff_lines_added_minus():
    """diff_lines 口径：只数 /^[+-]/ 变更行、跳过 +++/--- hunk 头，新增+删除合计。"""
    from types import SimpleNamespace

    from codereview_ai.worker import _count_diff_lines

    diffs = [
        SimpleNamespace(diff="""\
--- a/old.py
+++ b/new.py
@@ -1,3 +1,4 @@
 context
+added line
-removed line
+another
"""),
        SimpleNamespace(diff="""\
--- a/one.txt
+++ b/one.txt
@@ -0,0 +1,1 @@
+only new
"""),
    ]
    # 3 个变更行（added/removed/added）+ 1（only new）= 4；---/+++ 头与 @@ 不计
    assert _count_diff_lines(diffs) == 4

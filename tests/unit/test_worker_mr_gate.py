"""MR 轨自动审查门控（§7.7 双轨对称）测试：未接线→原样审、默认关→skipped、DB/项目覆盖、force 绕过。

离线：ForgeAdapter fake（parse_merge_request + fetch_files）+ Reviewer fake 计数。
注入临时 SQLite 的 ReviewRepository 断言 skipped/completed 状态与 force_rerun 消费清理。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import sqlalchemy as sa

from codereview_ai.domain.models import (
    ChangeType,
    FileDiff,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
)
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.project_repo import ProjectConfig
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.storage.setting_repo import MR_REVIEW_DEFAULT_KEY, SettingRepository
from codereview_ai.worker import process_raw_event


class _FakeForge(ForgeAdapter):
    name = "gitlab"

    def __init__(self) -> None:
        self.summaries: list[str] = []

    def parse_merge_request(self, data: dict[str, Any]):
        oa = data.get("object_attributes") or {}
        if data.get("object_kind") != "merge_request" or not oa.get("iid"):
            return None
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
        return [FileDiff(
            old_path="a.py", new_path="a.py", diff="---\n+++\n@@ -0,0 +1,2 @@\n+ctx\n",
            additions=1, deletions=0, change_type=ChangeType.NEW_FILE)]

    async def post_inline(self, pr, comments):
        pass

    async def post_summary(self, pr, body):
        self.summaries.append(body)

    async def post_commit_status(self, pr, *, passed, description):
        pass


class _FakeReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, *, pr, commits_text, diffs):
        self.calls += 1
        result = ReviewResult(summary="MR 审查完成", scores=ReviewScores(
            correctness=30, security=22, practices=10, performance=4, commit_quality=3))
        result.findings.append(Finding(
            content="MR 轨发现的问题", category="bug", severity="high",
            existing_code="x", file="a.py", line=1))
        return result


def _mr_payload() -> bytes:
    return json.dumps({
        "object_kind": "merge_request",
        "object_attributes": {"action": "open", "iid": 7, "target_project_id": 7,
                              "title": "add feature", "last_commit": {"id": "h"}},
    }).encode()


# ── 未接线（mr_default_enabled=None）→ 不门控、原样审（存量路径回归护栏）──


async def test_mr_not_wired_reviews_unchanged():
    forge = _FakeForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _mr_payload())
    assert reviewer.calls == 1


async def test_mr_wired_default_off_skips(tmp_path):
    """接线且默认关（未落库）→ 真审 0 次，落 skipped(mr_disabled) 审计行。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)
    forge = _FakeForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _mr_payload(), engine=engine,
                            review_repo=review_repo, mr_default_enabled=False)
    assert reviewer.calls == 0 and forge.summaries == []
    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "mr"))
                ).scalar_one()
        assert task.state == "skipped" and task.skip_reason == "mr_disabled"
        assert task.error == "MR 自动审查未开启（默认关闭），仅记录未审查"
    await engine.dispose()


# ── 全局默认热读（DB mr_review_default）覆盖 env/接线值 ─────────────────


async def test_mr_db_on_overrides_off_default(tmp_path):
    """未传项目配置：DB 落库「开」优先于接线默认 false → 审。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = SettingRepository(engine)
    await repo.set(MR_REVIEW_DEFAULT_KEY, "1")
    forge = _FakeForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _mr_payload(), engine=engine,
                            review_repo=ReviewRepository(engine), mr_default_enabled=False)
    assert reviewer.calls == 1
    await engine.dispose()


async def test_mr_db_off_overrides_on_default(tmp_path):
    """DB 落库「关」压住接线默认 true → 不审。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = SettingRepository(engine)
    await repo.set(MR_REVIEW_DEFAULT_KEY, "0")
    forge = _FakeForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _mr_payload(), engine=engine,
                            review_repo=ReviewRepository(engine), mr_default_enabled=True)
    assert reviewer.calls == 0
    await engine.dispose()


# ── 项目级 mr_enabled 覆盖 ──────────────────────────────────────────────


async def test_mr_project_override_disables_when_global_on(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    forge = _FakeForge()
    reviewer = _FakeReviewer()

    async def cfg(p, r):
        return ProjectConfig(mr_enabled=False)

    await process_raw_event(forge, reviewer, _mr_payload(), engine=engine,
                            review_repo=ReviewRepository(engine), mr_default_enabled=True,
                            project_config_factory=cfg)
    assert reviewer.calls == 0
    await engine.dispose()


async def test_mr_project_override_enables_when_global_off(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    forge = _FakeForge()
    reviewer = _FakeReviewer()

    async def cfg(p, r):
        return ProjectConfig(mr_enabled=True)

    await process_raw_event(forge, reviewer, _mr_payload(), engine=engine,
                            review_repo=ReviewRepository(engine), mr_default_enabled=False,
                            project_config_factory=cfg)
    assert reviewer.calls == 1
    await engine.dispose()


# ── 手动补审：force_rerun 绕过门控 ─────────────────────────────────────


async def test_mr_force_rerun_bypasses_gate(tmp_path):
    """同 head 已 skipped(mr_disabled)，置 force_rerun（tasks.retry 行为）→ 绕过门控强审、
    完成后清标记并达 completed。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)
    payload = _mr_payload()

    # 第一遍：默认关 → skipped
    await process_raw_event(_FakeForge(), _FakeReviewer(), payload, engine=engine,
                            review_repo=review_repo, mr_default_enabled=False)

    # 模拟 tasks.retry_task 对 MR 任务置 force_rerun 后重入队
    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "mr"))
                ).scalar_one()
        assert task.state == "skipped" and task.skip_reason == "mr_disabled"
        task.force_rerun = True
        await s.commit()

    # 第二遍：仍默认关 + 已有审计行 → 因 force 强制重跑并达 completed、清标记
    forge = _FakeForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, payload, engine=engine,
                            review_repo=review_repo, mr_default_enabled=False)
    assert reviewer.calls == 1 and len(forge.summaries) == 1
    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "mr"))
                ).scalar_one()
        assert task.state == "completed"
        assert task.force_rerun is False  # 补审意图已消费清除
    await engine.dispose()
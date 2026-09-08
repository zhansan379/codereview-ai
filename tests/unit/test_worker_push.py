"""worker push 轨（§7.7）接线测试：开关/分支门控、幂等审计落库、单条总结回写。

离线：ForgeAdapter fake（parse_push + 三分支 + `post_commit_summary` 记录）+ Reviewer fake。
注入临时 SQLite 的 `ReviewRepository` 断言 review_task/review_finding 真落库、幂等跳过、
skipped/completed 状态流转。
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa

from codereview_ai.domain.models import (
    Category,
    ChangeType,
    FileDiff,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
    Severity,
)
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.forges.gitlab import parse_push_event_payload
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.project_repo import ProjectConfig
from codereview_ai.storage.setting_repo import PUSH_REVIEW_DEFAULT_KEY, SettingRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import PushGate, build_push_summary, process_raw_event

ALL_ZERO = "0000000000000000000000000000000000000000"


def _gl_push(**over) -> bytes:
    payload = {
        "object_kind": "push",
        "user_username": "alice",
        "project": {"id": 7, "path_with_namespace": "acme/widgets"},
        "ref": "refs/heads/main",
        "before": "aaaa",
        "after": "bbbb",
        "commits": [{"id": "bbbb", "message": "fix: push 审查"}],
    }
    payload.update(over)
    return json.dumps(payload).encode()


class _FakePushForge(ForgeAdapter):
    name = "gitlab"

    def __init__(self) -> None:
        self.summaries: list[str] = []
        self._changes: list[FileDiff] = [
            FileDiff(
                old_path="a.py", new_path="a.py",
                diff="---\n+++\n@@ +1 +2 @@\n+ctx\n+x\n",
                additions=1, deletions=0, change_type=ChangeType.MODIFIED,
                new_file_content="x\n"),
        ]

    def parse_merge_request(self, data: dict[str, Any]):
        return None

    def parse_push_event(self, data: dict[str, Any]):
        return parse_push_event_payload(data)

    async def fetch_pull_request(self, pr: PullRequest):
        return pr

    async def fetch_files(self, pr):
        return self._changes

    async def get_push_changes(self, ev):
        return self._changes

    async def get_first_commit_changes(self, ev):
        return self._changes

    async def post_inline(self, pr, comments):
        pass

    async def post_summary(self, pr, body):
        pass

    async def post_commit_summary(self, ev, text):
        self.summaries.append(text)


class _FakeReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, *, pr, commits_text, diffs):
        self.calls += 1
        result = ReviewResult(summary="push 审查完成", scores=ReviewScores(
            correctness=30, security=22, practices=10, performance=4, commit_quality=3))
        result.findings.append(Finding(
            content="push 轨发现的问题", category=Category.BUG, severity=Severity.HIGH,
            existing_code="x", file="a.py"))
        return result


# ── 门控：默认关 / 分支规则 / 删分支 ──────────────────────────────────────


async def test_push_skipped_when_default_off():
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _gl_push())
    assert reviewer.calls == 0 and forge.summaries == []


async def test_push_review_gated_by_branch_match():
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    gate = PushGate(enabled=True, branch_match=lambda b: b == "main")
    await process_raw_event(forge, reviewer, _gl_push(), push_gate=gate)
    assert reviewer.calls == 1 and len(forge.summaries) == 1
    assert "总分 69" in forge.summaries[0]


async def test_push_review_branch_not_matched_skips():
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    gate = PushGate(enabled=True, branch_match=lambda b: b == "release")
    await process_raw_event(forge, reviewer, _gl_push(), push_gate=gate)
    assert reviewer.calls == 0 and forge.summaries == []


async def test_push_delete_branch_never_reviews():
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    gate = PushGate(enabled=True)
    await process_raw_event(forge, reviewer, _gl_push(after=ALL_ZERO), push_gate=gate)
    assert reviewer.calls == 0 and forge.summaries == []


async def test_push_new_branch_uses_first_commit_changes():
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    gate = PushGate(enabled=True)
    await process_raw_event(forge, reviewer, _gl_push(before=ALL_ZERO), push_gate=gate)
    assert reviewer.calls == 1 and len(forge.summaries) == 1


# ── 幂等 + 真落库（临时 SQLite）────────────────────────────────────────────


async def test_push_records_task_and_findings(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)

    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _gl_push(), push_gate=PushGate(enabled=True),
                            review_repo=review_repo)

    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "push"))
                ).scalar_one()
        assert task.state == "completed" and task.branch == "main"
        assert task.head_sha == "bbbb" and task.pr_number is None
        finding = (await s.execute(sa.select(ReviewFinding).where(ReviewFinding.task_id == task.id))
                   ).scalar_one()
        assert finding.source == "llm" and finding.category == str(Category.BUG)
        assert finding.severity == str(Severity.HIGH)
    await engine.dispose()


async def test_push_idempotent_via_unique_index(tmp_path):
    """同一 branch+after 再来一次：ensure_task 幂等 → 跳过审查，审计行唯一。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)

    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    gate = PushGate(enabled=True)
    raw = _gl_push()
    await process_raw_event(forge, reviewer, raw, push_gate=gate, review_repo=review_repo)
    await process_raw_event(forge, reviewer, raw, push_gate=gate, review_repo=review_repo)

    async with session_factory(engine)() as s:
        tasks = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "push"))
                 ).scalars().all()
        assert len(tasks) == 1
    assert reviewer.calls == 1  # 只真审了一次
    await engine.dispose()


async def test_push_default_off_records_skipped_audit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)

    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _gl_push(), review_repo=review_repo)  # 默认关

    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "push"))
                ).scalar_one()
        assert task.state == "skipped"
        # 默认关闭时跳过也要带人话原因，后台可直接展示（不再是一串空 error）
        assert task.error == "push 审查未开启（默认关闭），仅记录未审查"
        # skipped 分型：门控/配置类 → 可供「补审」（DESIGN §7.7）
        assert task.skip_reason == "push_disabled"
    assert reviewer.calls == 0 and forge.summaries == []
    await engine.dispose()


async def test_push_global_default_db_on_overrides_off_env_gate(tmp_path):
    """全局默认热读 app_setting：落库「开」优先于 env/push_gate 的关闭默认。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = SettingRepository(engine)
    await repo.set(PUSH_REVIEW_DEFAULT_KEY, "1")  # DB 默认开，即使未传 push_gate
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _gl_push(), engine=engine)
    assert reviewer.calls == 1 and len(forge.summaries) == 1
    await engine.dispose()


async def test_push_global_default_db_off_overrides_on_env_gate(tmp_path):
    """全局默认热读 app_setting：落库「关」能压住开启的 push_gate（env 兜底）。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = SettingRepository(engine)
    await repo.set(PUSH_REVIEW_DEFAULT_KEY, "0")  # 显式关
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, _gl_push(),
                            push_gate=PushGate(enabled=True), engine=engine)
    assert reviewer.calls == 0 and forge.summaries == []
    await engine.dispose()


# ── 项目级 push 开关（覆盖全局 env 默认）+ 手动补审绕过 ─────────────────────


async def test_push_project_override_enables_when_global_off():
    """全局默认关，但项目 `push_enabled=True` → 该项目 push 轨开门（DESIGN §7.7 项目覆盖）。"""
    forge = _FakePushForge()
    reviewer = _FakeReviewer()

    async def cfg(p, r):
        return ProjectConfig(push_enabled=True)

    await process_raw_event(forge, reviewer, _gl_push(), project_config_factory=cfg)
    assert reviewer.calls == 1 and len(forge.summaries) == 1


async def test_push_project_override_disables_when_global_on():
    """全局门开，但项目 `push_enabled=False` → 该项目 push 轨关闭，标 skipped。"""
    forge = _FakePushForge()
    reviewer = _FakeReviewer()

    async def cfg(p, r):
        return ProjectConfig(push_enabled=False)

    await process_raw_event(forge, reviewer, _gl_push(), push_gate=PushGate(enabled=True),
                            project_config_factory=cfg)
    assert reviewer.calls == 0 and forge.summaries == []


async def test_push_force_rerun_bypasses_gate_and_idempotency(tmp_path):
    """手动补审：同 (branch, after) 已 skipped 且 force_rerun=true → 绕过幂等预检 + 门控强审。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    review_repo = ReviewRepository(engine)
    raw = _gl_push()

    # 第一遍：默认关 → 落 skipped(push_disabled)
    await process_raw_event(_FakePushForge(), _FakeReviewer(), raw, review_repo=review_repo)

    # 模拟 retry_task 对 push 任务置 force_rerun 标记
    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "push"))
                ).scalar_one()
        assert task.state == "skipped"
        task.force_rerun = True
        await s.commit()

    # 第二遍：仍默认关 + 已有审计行 → 因 force 强制重跑并达 completed
    forge = _FakePushForge()
    reviewer = _FakeReviewer()
    await process_raw_event(forge, reviewer, raw, review_repo=review_repo)
    assert reviewer.calls == 1 and len(forge.summaries) == 1

    async with session_factory(engine)() as s:
        task = (await s.execute(sa.select(ReviewTask).where(ReviewTask.event_type == "push"))
                ).scalar_one()
        assert task.state == "completed"
        assert task.force_rerun is False  # 补审意图已消费清除
    await engine.dispose()


# ── build_push_summary 文本 ───────────────────────────────────────────────


def test_build_push_summary_embeds_score_and_findings():
    from codereview_ai.forges.gitlab import parse_push_event_payload

    ev = parse_push_event_payload({
        "object_kind": "push",
        "project": {"id": 7, "path_with_namespace": "acme/widgets"},
        "ref": "refs/heads/main", "before": "a", "after": "bbbb",
        "commits": [{"id": "bbbb", "message": "fix: x"}],
    })
    result = ReviewResult(summary="审查总结", scores=ReviewScores(
        correctness=30, security=22, practices=10, performance=4, commit_quality=3))
    result.findings.append(Finding(content="问题A", category=Category.BUG, severity=Severity.HIGH,
                                   existing_code="", file="a.py"))
    md = build_push_summary(ev, result)
    assert "acme/widgets@main" in md
    assert "总分 69" in md  # 30+22+10+4+3
    assert "问题A" in md

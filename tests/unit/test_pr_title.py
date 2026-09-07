"""review_task.pr_title 落库 + 存量回填提取（github/gitlab payload）。

- `ensure_task` 落 `pr_title`（默认空）。
- 回填脚本从 payload 提取 title：github `pull_request.title`、gitlab `object_attributes.title`、
  顶层 `title` 兜底；都无 → 跳过留空；幂等二跑 0。
"""

from __future__ import annotations

import sqlalchemy as sa

from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository

from scripts.backfill_pr_titles import main as backfill_main


async def _make_task(engine, *, payload: str | None = None, provider="github",
                     head_sha="h") -> int:
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(provider=provider, repo_id="9", pr_number=7, event_type="mr",
                       branch="f", head_sha=head_sha, pr_title="", payload=payload or "")
        s.add(t)
        await s.commit()
        return t.id


async def test_ensure_task_persists_pr_title(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await repo.ensure_task(
        provider="gitlab", repo_id="9", pr_number=7, event_type="mr", branch="f",
        head_sha="h", base_sha="b", pr_title="新增标题列",
    )
    assert tid is not None
    async with session_factory(engine)() as s:
        row = (await s.execute(sa.select(ReviewTask).where(ReviewTask.id == tid))).scalars().one()
    assert row.pr_title == "新增标题列"


async def test_ensure_task_pr_title_defaults_empty(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await repo.ensure_task(
        provider="gitlab", repo_id="9", pr_number=8, event_type="mr", branch="f", head_sha="h",
    )
    async with session_factory(engine)() as s:
        row = (await s.execute(sa.select(ReviewTask).where(ReviewTask.id == tid))).scalars().one()
    assert row.pr_title == ""


# ── 回填提取 ────────────────────────────────────────────────────────────────


async def test_backfill_extracts_github_and_gitlab_titles(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    # github payload
    await _make_task(engine, provider="github", head_sha="h1",
                     payload='{"pull_request":{"title":"github 标题","number":7}}')
    # gitlab payload
    await _make_task(engine, provider="gitlab", head_sha="h2",
                     payload='{"object_attributes":{"title":"gitlab 标题","iid":2}}')
    # 顶层 title 兜底 + 全无标题
    await _make_task(engine, provider="github", head_sha="h3", payload='{"title":"顶层标题"}')
    await _make_task(engine, provider="gitlab", head_sha="h4",
                     payload='{"action":"update"}')  # 无标题 → 跳过

    await backfill_main(f"sqlite:///{tmp_path}/t.db")

    async with session_factory(engine)() as s:
        rows = (await s.execute(sa.select(ReviewTask).order_by(ReviewTask.id))).scalars().all()
    assert [r.pr_title for r in rows] == ["github 标题", "gitlab 标题", "顶层标题", ""]

    # 幂等：title 已回填与非 mr 无关；无标题行仍空，但已处理不再改动 → 二跑 0
    # （_make_task 的 4 行：3 行有 title、1 行空；回填只挑 pr_title='' 的行）
    await backfill_main(f"sqlite:///{tmp_path}/t.db")
    async with session_factory(engine)() as s:
        rows = (await s.execute(sa.select(ReviewTask).order_by(ReviewTask.id))).scalars().all()
    assert [r.pr_title for r in rows] == ["github 标题", "gitlab 标题", "顶层标题", ""]


async def test_backfill_skips_non_mr_rows(tmp_path):
    """push 轨（event_type='push'）不在回填范围内。"""
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    async with session_factory(engine)() as s:
        t = ReviewTask(provider="github", repo_id="9", pr_number=None, event_type="push",
                       branch="f", head_sha="h", pr_title="", payload='{"title":"不应动"}')
        s.add(t)
        await s.commit()
    await backfill_main(f"sqlite:///{tmp_path}/t.db")
    async with session_factory(engine)() as s:
        row = (await s.execute(sa.select(ReviewTask))).scalars().one()
    assert row.pr_title == ""
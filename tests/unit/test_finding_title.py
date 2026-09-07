"""finding 标题 AI 化：表格短标题 + 折叠面板完整分析。

- `insert_findings` 把 `Finding.title` 落 title、`content` 落 detail；无 title 时兜底截断。
- `_coerce_finding` 从 LLM JSON 读 title（缺失→空串兜底）。
- `scripts/backfill_finding_titles.py` 存量回填：假 backend 离线断言 title 写回 + 幂等。
"""

from __future__ import annotations

import sqlalchemy as sa

from codereview_ai.domain.models import Category, Finding, Severity
from codereview_ai.review.llm_gateway import _coerce_finding
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask
from codereview_ai.storage.review_repo import ReviewRepository

from scripts.backfill_finding_titles import _Backend, main as backfill_main

MR = dict(provider="gitlab", repo_id="9", pr_number=7, event_type="mr", branch="f")


def _finding(content: str, file: str, title: str = "") -> Finding:
    return Finding(content=content, category=Category.BUG, severity=Severity.HIGH,
                   existing_code="", file=file, title=title)


async def _seed_task(engine, head_sha="h") -> int:
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(**MR, head_sha=head_sha, state="completed")
        s.add(t)
        await s.commit()
        return t.id


async def _only_finding(engine) -> ReviewFinding:
    session = session_factory(engine)
    async with session() as s:
        return (await s.execute(sa.select(ReviewFinding))).scalars().one()


# ── _coerce_finding 解析 title ───────────────────────────────────────────────


def test_coerce_finding_reads_title_and_copies_content():
    f = _coerce_finding({"content": "完整分析", "file": "a.py",
                         "title": "短标题", "category": "bug", "severity": "high"})
    assert f is not None
    assert f.title == "短标题"
    # content 原样保留在 Finding（detail=content），title 独立
    assert f.content == "完整分析" and f.title != f.content


def test_coerce_finding_title_missing_falls_back_empty():
    f = _coerce_finding({"content": "完整分析", "file": "a.py"})
    assert f is not None and f.title == ""


# ── insert_findings 落库拆分 ─────────────────────────────────────────────────


async def test_insert_findings_splits_title_detail(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await _seed_task(engine)
    await repo.insert_findings(tid, [_finding("完整分析内容", "a.py", title="短标题")])
    row = await _only_finding(engine)
    assert row.title == "短标题"
    assert row.detail == "完整分析内容"
    assert row.title != row.detail


async def test_insert_findings_title_fallback_truncates(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    repo = ReviewRepository(engine)
    tid = await _seed_task(engine)
    long = "非常长的完整分析" * 20
    await repo.insert_findings(tid, [_finding(long, "a.py")])  # 无 title
    row = await _only_finding(engine)
    assert row.title == long[:39] + "…"
    assert row.detail == long  # 完整分析未丢
    assert len(row.title) <= 40


# ── 回填脚本（假 backend，离线）───────────────────────────────────────────────


async def test_backfill_replaces_fulltext_title_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    await init_db(engine)
    tid = await _seed_task(engine)
    full = "存量全文分析" * 30  # > 40 字符，回填前的状态
    async with session_factory(engine)() as s:
        s.add(ReviewFinding(task_id=tid, fingerprint="fp", severity="high",
                            category="bug", file="a.py", title=full, detail=full))
        await s.commit()

    fake = _Backend(["回填短标题"])
    await backfill_main(f"sqlite:///{tmp_path}/t.db", "test-model", fake)

    async with session_factory(engine)() as s:
        row = (await s.execute(sa.select(ReviewFinding))).scalars().one()
    assert row.title == "回填短标题"
    assert row.detail == full  # 完整分析不动

    # 幂等：title 已 != detail 且更短，二跑 0 变更
    fake2 = _Backend(["另一个标题"])
    await backfill_main(f"sqlite:///{tmp_path}/t.db", "test-model", fake2)
    async with session_factory(engine)() as s:
        row2 = (await s.execute(sa.select(ReviewFinding))).scalars().one()
    assert row2.title == "回填短标题"
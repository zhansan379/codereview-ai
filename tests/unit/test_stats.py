"""看板统计 /api/stats（DESIGN §14.1）单测：聚合数值 + JWT 鉴权。

离线：临时 SQLite 播种 ReviewTask/ReviewFinding/ModelUsage，断言服务端 GROUP BY
各聚合正确；无 token 401。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai.api.admin import stats
from codereview_ai.api.auth import issue_token
from codereview_ai.api.auth import router as auth_router
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import (
    ModelUsage,
    ReviewConversation,
    ReviewFinding,
    ReviewTask,
)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'stats.db'}")
    await init_db(engine)
    settings = type("S", (), {"secret_key": "s", "encryption_key": _fernet_key()})()
    async with session_factory(engine)() as s:
        s.add_all([
            # 两个状态分布
            ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                       head_sha="a1", state="completed", score_total=85),
            ReviewTask(provider="gitlab", repo_id="2", pr_number=2, event_type="mr",
                       head_sha="a2", state="failed"),
            ReviewTask(provider="github", repo_id="3", pr_number=None, event_type="push",
                       head_sha="a3", state="skipped"),
        ])
        await s.commit()

        t1 = (await s.execute(select(ReviewTask).where(ReviewTask.head_sha == "a1"))
              ).scalar_one()
        s.add_all([
            ReviewFinding(task_id=t1.id, fingerprint="f1", severity="high", category="bug",
                          source="llm", status="active"),
            ReviewFinding(task_id=t1.id, fingerprint="f2", severity="critical", category="security",
                          source="llm", status="active"),
            ReviewFinding(task_id=t1.id, fingerprint="f3", severity="critical", category="security",
                          source="static:semgrep", status="active"),
            ReviewFinding(task_id=t1.id, fingerprint="f4", severity="critical", category="security",
                          source="llm", status="resolved"),  # resolved 不计入 open_critical
        ])
        s.add_all([
            ModelUsage(task_id=1, model="gpt-4o", prompt_tokens=100, completion_tokens=50,
                       total_tokens=150),
            ModelUsage(task_id=1, model="gpt-4o", prompt_tokens=40, completion_tokens=20,
                       total_tokens=60),
            ModelUsage(task_id=2, model="claude", prompt_tokens=10, completion_tokens=5,
                       total_tokens=15),
        ])
        await s.commit()

    fast = FastAPI()
    fast.state.engine = engine
    fast.state.settings = settings
    fast.include_router(auth_router, prefix="/api")
    fast.include_router(stats.router, prefix="/api")
    token = issue_token(settings.secret_key)
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _client(fast: FastAPI, token: str) -> TestClient:
    return TestClient(fast, headers={"Authorization": f"Bearer {token}"})


def test_stats_aggregates(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.get("/api/stats")
        assert r.status_code == 200
        d = r.json()

        assert d["total_tasks"] == 3
        assert d["total_findings"] == 4
        assert d["open_critical"] == 2  # critical 且 active（第三个 resolved 排除）
        assert d["open_high"] == 1

        states = {i["key"]: i["count"] for i in d["tasks_by_state"]}
        assert states == {"completed": 1, "failed": 1, "skipped": 1}

        sev = {i["key"]: i["count"] for i in d["findings_by_severity"]}
        assert sev["critical"] == 3 and sev["high"] == 1

        cat = {i["key"]: i["count"] for i in d["findings_by_category"]}
        assert cat["security"] == 3 and cat["bug"] == 1

        prov = {i["key"]: i["count"] for i in d["provider_split"]}
        assert prov == {"gitlab": 2, "github": 1}

        # 近 14 天每日审查量始终 14 天（服务端补零），今天应有 3
        assert len(d["reviews_by_day"]) == 14
        today = datetime.now(UTC).date().isoformat()
        by_day = {i["day"]: i["count"] for i in d["reviews_by_day"]}
        assert by_day[today] == 3

        usage = {i["model"]: i for i in d["model_usage"]}
        assert usage["gpt-4o"]["requests"] == 2
        assert usage["gpt-4o"]["prompt_tokens"] == 140
        assert usage["gpt-4o"]["total_tokens"] == 210
        assert usage["claude"]["requests"] == 1

        # 播种行未真正执行审查（exec_mode=NULL）→ 模式维度被过滤为空，不误计入 diff
        modes = {i["key"]: i["count"] for i in d["tasks_by_mode"]}
        assert modes == {}
        assert d["agent_task_count"] == 0
        assert d["avg_chat_rounds"] == 0
        assert d["agent_scatter"] == []


async def test_stats_agent_mode(tmp_path):
    """agent 模式维度 + 复杂度×成本散点聚合：
    `agent_scatter` 只收完成、时间齐全的 agentic 任务；duration_s 取 started→finished。
    """
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    await init_db(engine)
    now = datetime.now(UTC)
    async with session_factory(engine)() as s:
        s.add_all([
            # agentic 完成：有完整时间窗口 + 复杂度/轮数 → 进散点
            ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                       head_sha="a1", state="completed", exec_mode="agentic",
                       diff_lines=120, chat_rounds=8, tool_calls=15,
                       started_at=now, finished_at=now),
            # agentic 但 failed：时间无意义，散点必须排除
            ReviewTask(provider="gitlab", repo_id="2", pr_number=2, event_type="mr",
                       head_sha="a2", state="failed", exec_mode="agentic",
                       diff_lines=50, chat_rounds=3, tool_calls=4,
                       started_at=now, finished_at=now),
            # diff 完成：即便有数据也不许进 agent_scatter
            ReviewTask(provider="github", repo_id="3", pr_number=3, event_type="mr",
                       head_sha="a3", state="completed", exec_mode="diff",
                       diff_lines=999, chat_rounds=0, tool_calls=0,
                       started_at=now, finished_at=now),
        ])
        # 制造 60s 响应时长（agentic completed 那条）
        row = (await s.execute(
            select(ReviewTask).where(ReviewTask.head_sha == "a1")
        )).scalar_one()
        row.finished_at = now + timedelta(seconds=60)
        await s.commit()
        out = await stats.aggregate_stats(s)

    assert {i.key: i.count for i in out.tasks_by_mode} == {"agentic": 2, "diff": 1}
    assert out.agent_task_count == 2
    assert out.avg_chat_rounds == 5.5  # (8 + 3) / 2

    # 只有 agentic+completed 的 a1 进散点；a2(failed)、a3(diff) 都排除
    assert len(out.agent_scatter) == 1
    point = out.agent_scatter[0]
    assert point.diff_lines == 120
    assert point.duration_s == 60
    assert point.chat_rounds == 8
    assert point.tool_calls == 15
    await engine.dispose()


async def test_stats_mode_filter_phase_duration(tmp_path):
    """mode=agentic/diff 过滤通用图 + KPI；phase_box 分布聚合；duration_by_day 按完成日分桶。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'mode.db'}")
    await init_db(engine)
    now = datetime.now(UTC)
    started = now - timedelta(seconds=120)
    async with session_factory(engine)() as s:
        s.add_all([
            # agentic 完成（今天完成，120s）→ 进散点、进 agentic 计数与耗时
            ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                       head_sha="a4", state="completed", exec_mode="agentic",
                       chat_rounds=2, diff_lines=30, started_at=started, finished_at=now),
            # diff 完成 → 只进 diff 计数与耗时，不进散点
            ReviewTask(provider="github", repo_id="2", pr_number=2, event_type="mr",
                       head_sha="a5", state="completed", exec_mode="diff",
                       started_at=started, finished_at=now),
            # agentic 失败（无时间戳）→ 计入 agentic 任务数与平均轮数分母，不进耗时
            ReviewTask(provider="github", repo_id="3", pr_number=3, event_type="mr",
                       head_sha="a6", state="failed", exec_mode="agentic"),
        ])
        await s.commit()
        a4 = (await s.execute(select(ReviewTask).where(ReviewTask.head_sha == "a4"))).scalar_one()
        s.add_all([
            ReviewConversation(task_id=a4.id, seq=0, phase="plan"),
            ReviewConversation(task_id=a4.id, seq=1, phase="main"),
            ReviewConversation(task_id=a4.id, seq=2, phase="main"),
            ReviewConversation(task_id=a4.id, seq=3, phase="scoring"),
        ])
        await s.commit()

        out_all = await stats.aggregate_stats(s)
        out_agent = await stats.aggregate_stats(s, mode="agentic")
        out_diff = await stats.aggregate_stats(s, mode="diff")

    # 模式过滤：agentic 只看 agent 任务（a4+a6）；diff 只看 diff（a5）；all 全量
    assert out_all.total_tasks == 3
    assert out_agent.total_tasks == 2
    assert out_diff.total_tasks == 1

    # agent 专属度量恒全量（不随开关）：散点只收 agentic+completed → a4；平均轮数含 failed 分母
    scatter_counts = {
        len(out_agent.agent_scatter),
        len(out_diff.agent_scatter),
        len(out_all.agent_scatter),
    }
    assert scatter_counts == {1}
    assert out_agent.avg_chat_rounds == (2 + 0) / 2  # a4 chat_rounds=2, a6=0

    # 阶段管线：箱线图分布聚合，恒全量（不随 mode 变化）
    def as_dict(b) -> dict:
        return {"key": b.key, "task_count": b.task_count, "min": b.min, "q1": b.q1,
                "median": b.median, "q3": b.q3, "max": b.max, "mean": b.mean,
                "mode": b.mode}

    # a4 = plan×1, main×2, scoring×1 → 各 phase 都只有单值
    expect = {
        "plan": {"key": "plan", "task_count": 1, "min": 1, "q1": 1.0, "median": 1.0,
                 "q3": 1.0, "max": 1, "mean": 1.0, "mode": 1},
        "main": {"key": "main", "task_count": 1, "min": 2, "q1": 2.0, "median": 2.0,
                 "q3": 2.0, "max": 2, "mean": 2.0, "mode": 2},
        "scoring": {"key": "scoring", "task_count": 1, "min": 1, "q1": 1.0,
                    "median": 1.0, "q3": 1.0, "max": 1, "mean": 1.0, "mode": 1},
    }
    assert {d["key"]: d for d in map(as_dict, out_all.phase_box)} == expect
    assert {d["key"]: d for d in map(as_dict, out_agent.phase_box)} == expect  # 不随模式变化

    # duration_by_day：今天完成 → all 两任务、agentic 只 a4；avg 120s
    today_iso = now.date().isoformat()
    today_all = next(d for d in out_all.duration_by_day if d.day == today_iso)
    assert today_all.count == 2 and today_all.avg_seconds == 120.0
    today_agent = next(d for d in out_agent.duration_by_day if d.day == today_iso)
    assert today_agent.count == 1 and today_agent.avg_seconds == 120.0

    await engine.dispose()


def test_stats_requires_auth(app):
    fast, _token = app
    assert TestClient(fast).get("/api/stats").status_code == 401


async def test_phase_box_distribution(tmp_path):
    """阶段管线箱线图聚合：按 (task, phase) 分组后，phase 内聚合 min/分位/mean/mode。

    播种 4 个 task，`main` 阶段各自 1/1/3/3 次调用 → 分布 [1,1,3,3]：
    min=1 max=3 mean=2 mode=1（1 与 3 并列取最小）q1=1 median=2 q3=3 task_count=4。
    """
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'box.db'}")
    await init_db(engine)
    async with session_factory(engine)() as s:
        tasks = [
            ReviewTask(provider="gitlab", repo_id="1", pr_number=1, event_type="mr",
                       head_sha=f"h{i}", state="completed", exec_mode="agentic")
            for i in range(4)
        ]
        s.add_all(tasks)
        await s.commit()
        ids = [t.id for t in tasks]
        # main 分桶：1,1,3,3 → 每条一次 llm.chat()
        counts = [1, 1, 3, 3]
        convs = []
        for tid, n in zip(ids, counts):
            convs += [ReviewConversation(task_id=tid, seq=i, phase="main")
                      for i in range(n)]
        # scoring 只出现在 task0，2 次调用（验证多 phase 各自独立聚合）
        convs += [ReviewConversation(task_id=ids[0], seq=10 + i, phase="scoring")
                  for i in range(2)]
        s.add_all(convs)
        await s.commit()
        out = await stats.aggregate_stats(s)
    await engine.dispose()

    by = {b.key: b for b in out.phase_box}
    assert set(by) == {"main", "scoring"}
    m = by["main"]
    assert (m.task_count, m.min, m.max, m.mean, m.mode) == (4, 1, 3, 2.0, 1)
    assert (m.q1, m.median, m.q3) == (1.0, 2.0, 3.0)
    sc = by["scoring"]
    assert (sc.task_count, sc.min, sc.max, sc.mode) == (1, 2, 2, 2)

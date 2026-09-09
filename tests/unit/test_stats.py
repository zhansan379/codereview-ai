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
from codereview_ai.storage.models import ModelUsage, ReviewFinding, ReviewTask


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

        # 未显式指定 exec_mode 的播种行默认 diff → 模式维度全落 diff
        modes = {i["key"]: i["count"] for i in d["tasks_by_mode"]}
        assert modes == {"diff": 3}
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


def test_stats_requires_auth(app):
    fast, _token = app
    assert TestClient(fast).get("/api/stats").status_code == 401

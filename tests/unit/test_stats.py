"""看板统计 /api/stats（DESIGN §14.1）单测：聚合数值 + JWT 鉴权。

离线：临时 SQLite 播种 ReviewTask/ReviewFinding/ModelUsage，断言服务端 GROUP BY
各聚合正确；无 token 401。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from codereview_ai.api.admin import stats
from codereview_ai.storage.models import ModelUsage, ReviewFinding, ReviewTask
from tests.unit.helpers import make_admin_app


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    async def seed(s):
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

    fast, token, _admin, engine = await make_admin_app(
        tmp_path, db_name="stats.db", routers=[stats.router], seed=seed,
    )
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


def test_stats_requires_auth(app):
    fast, _token = app
    assert TestClient(fast).get("/api/stats").status_code == 401

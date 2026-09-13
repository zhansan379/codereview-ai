"""提交时间 · 工作辛苦度分析 /api/stats/workrate 单测：事件抽取 + 聚合 + 鉴权。

离线：临时 SQLite 播种 MR/push 两轨任务（push 行带原始 webhook payload），
断言小时/星期/月份分桶（含时区平移）、连续天数、六维指数、用户排行、
作者/项目过滤与 days 窗口；无 token 401。日期用固定历史值（days=0 不限窗口）
保证断言与运行时钟无关；days 窗口单独用相对 now 播种验证。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codereview_ai.api.admin import workrate
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import Project, ReviewTask
from tests.unit.helpers import make_admin_app

#: 东八区的 getTimezoneOffset() 值（分钟）
TZ_CN = -480

REPO1 = {"provider": "gitea", "repo_id": "org/repo1"}
REPO2 = {"provider": "gitea", "repo_id": "org/repo2"}

PUSH_PAYLOAD = json.dumps({
    "ref": "refs/heads/main",
    "pusher": {"name": "bob"},
    "commits": [
        {"id": "abc", "message": "m1", "author": {"name": "bob"},
         "timestamp": "2026-09-08T09:30:00Z"},
        # 带时区偏移的写法：+08:00 → UTC 2026-09-08T18:30（本地东八区 09-09 02:30）
        {"id": "def", "message": "m2", "author": {"name": "alice"},
         "timestamp": "2026-09-09T02:30:00+08:00"},
    ],
})
PUSH_EMPTY_PAYLOAD = json.dumps({"ref": "refs/heads/dev", "pusher": {"name": "carol"}})


_SHA = (f"s{i}" for i in range(1000))


def _mr(provider_repo: dict, author: str, at: datetime, project_id: int | None) -> ReviewTask:
    return ReviewTask(provider=provider_repo["provider"], repo_id=provider_repo["repo_id"],
                      project_id=project_id, pr_number=1, event_type="mr", head_sha=next(_SHA),
                      state="completed", pr_author=author, queued_at=at)


def _push(provider_repo: dict, payload: str, at: datetime, project_id: int | None) -> ReviewTask:
    return ReviewTask(provider=provider_repo["provider"], repo_id=provider_repo["repo_id"],
                      project_id=project_id, event_type="push", head_sha=next(_SHA),
                      state="completed", queued_at=at, payload=payload)


@pytest.fixture
async def app(tmp_path) -> AsyncIterator[tuple[FastAPI, str]]:
    """固定历史日期播种（东八区本地时间换算见各断言）。"""

    async def seed(s):
        s.add_all([
            Project(provider="gitea", repo_id="org/repo1", repo_full_name="org/repo1"),
            Project(provider="gitea", repo_id="org/repo2", repo_full_name="org/repo2"),
        ])
        s.add_all([
            # 09-07 周一 UTC 04:00 → 本地 12:00（工作时段）
            _mr(REPO1, "alice", datetime(2026, 9, 7, 4, 0), 1),
            # 09-07 周一 UTC 17:00 → 本地 09-08 周二 01:00（深夜，本地日期跨天）
            _mr(REPO1, "alice", datetime(2026, 9, 7, 17, 0), 1),
            # push 拆 2 条 commit（bob 09-08 17:30 工作 / alice 09-09 02:30 深夜）
            _push(REPO1, PUSH_PAYLOAD, datetime(2026, 9, 8, 10, 0), 1),
            # 无 commits 的 push：退化为 pusher 一条（09-10 周四本地 20:00 晚间）
            _push(REPO1, PUSH_EMPTY_PAYLOAD, datetime(2026, 9, 10, 12, 0), 1),
            # 09-12 周六 UTC 08:00 → 本地 16:00（周末）
            _mr(REPO1, "bob", datetime(2026, 9, 12, 8, 0), 1),
            # 空 pr_author 的 MR：不进统计
            _mr(REPO1, "", datetime(2026, 9, 11, 8, 0), 1),
            # 项目 2：alice 09-09 周三本地 11:00
            _mr(REPO2, "alice", datetime(2026, 9, 9, 3, 0), 2),
        ])
        await s.commit()

    fast, token, _admin, engine = await make_admin_app(
        tmp_path, db_name="workrate.db", routers=[workrate.router], seed=seed,
    )
    try:
        yield fast, token
    finally:
        await engine.dispose()


def _client(fast: FastAPI, token: str) -> TestClient:
    return TestClient(fast, headers={"Authorization": f"Bearer {token}"})


# ── 事件抽取（纯函数）────────────────────────────────────────────────────────


def test_events_gitlab_style_and_fallback():
    """GitLab 风格 payload：commits[].author.name 优先；删分支退化为 pusher。"""
    payload = json.dumps({
        "object_kind": "push", "user_username": "lee",
        "commits": [{"id": "a", "message": "x", "author": {"name": "王五"},
                     "timestamp": "2026-09-10T02:00:00Z"}],
    })
    events = workrate._events_from_row(
        "push", "", datetime(2026, 9, 10, 3, 0), payload)
    assert len(events) == 1
    assert events[0].author == "王五"
    assert events[0].ts == datetime(2026, 9, 10, 2, 0)

    # 无 commits（删分支）：一条 pusher 事件，时间退化为入队时间
    events = workrate._events_from_row(
        "push", "", datetime(2026, 9, 10, 3, 0), json.dumps({"user_name": "lee"}))
    assert len(events) == 1
    assert events[0].author == "lee"
    assert events[0].ts == datetime(2026, 9, 10, 3, 0)

    # 既无 commits 也无 pusher：不产出事件
    assert workrate._events_from_row("push", "", datetime(2026, 9, 10, 3, 0), "{}") == []
    # 坏 payload：不抛
    assert workrate._events_from_row("push", "", datetime(2026, 9, 10, 3, 0), "not-json") == []


# ── 报告聚合（端到端）────────────────────────────────────────────────────────


def test_workrate_report_aggregates(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.get("/api/stats/workrate/report", params={"tz": TZ_CN, "days": 0})
        assert r.status_code == 200
        d = r.json()

        # 7 条事件（2 MR + push 拆 2 + 退化 1 + 1 MR + 1 MR；空作者排除）
        assert d["scope"]["commits"] == 7
        assert d["scope"]["projects_count"] == 2

        kpi = d["kpi"]
        assert kpi["total"] == 7
        # 本地日期：07/08/09/10/12 → 5 个活跃天，跨度 6 天
        assert kpi["active_days"] == 5
        assert kpi["daily_avg"] == 1.4
        # 07-10 连续 4 天；12 号断开
        assert kpi["longest_streak"] == 4
        assert kpi["streak_from"] == "2026-09-07"
        assert kpi["streak_to"] == "2026-09-10"
        # 深夜（本地 01:00 / 02:30）2 条；周末（周六）1 条；夜间或周末 3 条
        assert kpi["late_night"] == 2
        assert kpi["late_night_pct"] == 28.6
        assert kpi["night"] == 2
        assert kpi["non_work"] == 3
        assert kpi["weekend"] == 1
        assert kpi["weekend_pct"] == 14.3
        assert kpi["night_or_weekend"] == 3

        # 24 小时分布：本地小时 1/2/11/12/17/20 各 1，时段带正确
        by_hour = {h["hour"]: h for h in d["hourly"]}
        assert len(d["hourly"]) == 24
        assert by_hour[1]["band"] == "deep"
        assert by_hour[2]["band"] == "deep"
        assert by_hour[11]["band"] == "work"
        assert by_hour[12]["band"] == "work"
        assert by_hour[17]["band"] == "work"
        assert by_hour[20]["band"] == "evening"

        # 星期分布（1=周一）：周一1 周二2 周三2 周四1 周六1
        wd = {w["day"]: w["count"] for w in d["weekday"]}
        assert wd == {1: 1, 2: 2, 3: 2, 4: 1, 5: 0, 6: 1, 7: 0}

        # 月度趋势
        assert d["monthly"] == [{"month": "2026-09", "count": 7}]

        # 用户排行：alice 4 / bob 3 / carol 1
        authors = [(a["name"], a["commits"]) for a in d["authors"]]
        assert authors == [("alice", 4), ("bob", 2), ("carol", 1)]
        alice = d["authors"][0]
        assert alice["active_days"] == 3
        assert alice["late_night"] == 2

        # 辛苦指数：六维权重 25/15/15/20/15/10，本组数据合计 64.2（重度拼搏）
        idx = d["index"]
        assert idx["level"] == "intense"
        assert idx["score"] == 64.2
        parts = {p["code"]: p for p in idx["parts"]}
        assert parts["deep_night"]["score"] == 25.0  # 28.6% ≥ 20% 拉满
        assert parts["night"]["score"] == 12.3
        assert parts["weekend"]["score"] == 8.6
        assert parts["streak"]["score"] == 5.7
        assert parts["density"]["score"] == 2.6
        assert parts["attendance"]["score"] == 10.0  # 出勤 83.3% ≥ 70% 拉满

        # 洞察/建议：结构 + 确定性触发
        codes = [i["code"] for i in d["insights"]]
        assert "dual_peak" in codes and "night_owl" in codes
        assert "top_weekday" in codes and "streak_info" in codes
        sugg = [s["code"] for s in d["suggestions"]]
        assert sugg == ["late_shift", "rest_ratio"]


def test_workrate_report_filters(app):
    """作者 / 项目 / 时区过滤：各自缩小口径，tz=0 时按 UTC 分桶。"""
    fast, token = app
    with _client(fast, token) as c:
        # 作者过滤：alice 4 条
        d = c.get("/api/stats/workrate/report",
                  params={"tz": TZ_CN, "days": 0, "author": "alice"}).json()
        assert d["scope"]["commits"] == 4
        assert d["kpi"]["active_days"] == 3
        assert [a["name"] for a in d["authors"]] == ["alice"]

        # 项目过滤：repo1（6 条，排除项目 2 的 1 条）
        d = c.get("/api/stats/workrate/report",
                  params={"tz": TZ_CN, "days": 0, "project_id": 1}).json()
        assert d["scope"]["commits"] == 6
        assert d["repos"][0]["name"] == "org/repo1"

        # tz=0：按 UTC 分桶，04:00 落深夜带而非本地 12 点
        d = c.get("/api/stats/workrate/report", params={"tz": 0, "days": 0}).json()
        by_hour = {h["hour"]: h for h in d["hourly"]}
        assert by_hour[4]["count"] == 1 and by_hour[4]["band"] == "deep"
        # UTC 03/04 点各 1（本地 01/02 点的那两条按 UTC 在 17/18 点）
        assert d["kpi"]["late_night"] == 2


def test_workrate_options(app):
    fast, token = app
    with _client(fast, token) as c:
        r = c.get("/api/stats/workrate/options", params={"days": 0})
        assert r.status_code == 200
        d = r.json()
        assert d["authors"] == [
            {"name": "alice", "count": 4},
            {"name": "bob", "count": 2},
            {"name": "carol", "count": 1},
        ]
        assert d["projects"] == [
            {"id": 1, "count": 6, "name": "org/repo1"},
            {"id": 2, "count": 1, "name": "org/repo2"},
        ]

        # 项目联动：只看项目 2 时，作者仅剩 alice
        d = c.get("/api/stats/workrate/options",
                  params={"days": 0, "project_id": 2}).json()
        assert d["authors"] == [{"name": "alice", "count": 1}]


async def test_workrate_days_window(tmp_path):
    """days 窗口：30 天只收近 30 天的任务；0 不限（直接驱动 `_load_events`）。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'workrate_days.db'}")
    await init_db(engine)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory(engine)() as s:
        s.add_all([
            _mr(REPO1, "alice", now - timedelta(days=40), 1),
            _mr(REPO1, "bob", now - timedelta(days=2), 1),
        ])
        await s.commit()
        recent = await workrate._load_events(s, is_global=True, ids=set(), days=30)
        assert [e.author for e in recent] == ["bob"]
        all_events = await workrate._load_events(s, is_global=True, ids=set(), days=0)
        assert {e.author for e in all_events} == {"alice", "bob"}
    await engine.dispose()


def test_workrate_requires_auth(app):
    fast, _token = app
    assert TestClient(fast).get("/api/stats/workrate/report").status_code == 401
    assert TestClient(fast).get("/api/stats/workrate/options").status_code == 401

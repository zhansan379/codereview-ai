"""提交时间 · 工作辛苦度分析（`GET /api/stats/workrate/*`，JWT + `stats:view`）。

复用统计看板的口径与隔离（DESIGN §14.1 同源）：数据全部来自 `review_task`——
- **MR 轨**：一条任务 = 一次 PR/MR 提交事件，作者取 `pr_author`、时间取 `queued_at`
  （webhook 即时入队 ≈ 提交审查时间；补拉的历史任务为补拉时间，口径注明）。
- **push 轨**：从落库的原始 webhook `payload` 还原每条 commit（`commits[].author.name`
  + `commits[].timestamp`），解析不出时退化为「pusher 在入队时间」一条事件。

所有时间分桶（24 小时 / 星期 / 月份 / 连续天数）按**前端传来的本地时区偏移**
（`tz` = JS `getTimezoneOffset()` 分钟数，东八区为 -480）平移后计算，深夜/周末判定
与用户墙钟一致。辛苦指数为六维加权（权重在 `_index_parts` 注释里），纯确定性公式，
不调用模型。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_project_ids,
    get_db,
    require_permission,
    review_scope_clause,
)
from codereview_ai.storage.models import Project, ReviewTask

# 与 /stats 同门槛：admin/tech_lead/viewer 具备；数据层另有项目成员隔离。
router = APIRouter(prefix="/stats/workrate", dependencies=[
    Depends(require_permission("stats:view")),
])

#: `days` 允许的窗口（0 = 不限）；其余值回落默认。
ALLOWED_DAYS = (0, 30, 60, 90, 180)
DEFAULT_DAYS = 90

# ── 时段口径（对齐参考报告的「关键指标」表）────────────────────────────────
#: 深夜：0-6 点（含 0，不含 6）
DEEP_NIGHT_HOURS = frozenset(range(0, 6))
#: 夜间：23 点-次日 6 点（23,0,1,2,3,4,5）——包含深夜
NIGHT_HOURS = frozenset({23, *range(0, 6)})
#: 非工作时段：18 点-次日 9 点（18-23 与 0-8）
NON_WORK_HOURS = frozenset({*range(18, 24), *range(0, 9)})
#: 周末（ISO weekday：6=周六 7=周日）
WEEKEND_DAYS = frozenset({6, 7})

#: 24 小时分布的时段带（互斥，供前端配色）：0-5 深夜 / 6-8 清晨 /
#: 9-17 工作时段 / 18-22 晚间 / 23 深夜23点
BAND_DEEP, BAND_DAWN, BAND_WORK, BAND_EVENING, BAND_NIGHT23 = (
    "deep", "dawn", "work", "evening", "night23",
)


def _hour_band(hour: int) -> str:
    if hour in DEEP_NIGHT_HOURS:
        return BAND_DEEP
    if hour <= 8:
        return BAND_DAWN
    if hour <= 17:
        return BAND_WORK
    if hour <= 22:
        return BAND_EVENING
    return BAND_NIGHT23


# ── 事件抽取 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Event:
    """一条提交事件：作者 + UTC naive 时间 + 归属项目（payload 抽取阶段可缺省）。"""

    author: str
    ts: datetime  # naive UTC
    project_id: int | None = None
    repo_label: str = ""


def _as_utc_naive(dt: datetime) -> datetime:
    """统一成 naive UTC：DB 读回 SQLite 是 naive（墙钟即 UTC）；aware 则先换算再脱 tz。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _parse_iso(value: Any) -> datetime | None:
    """容错解析 webhook 里的 ISO8601 时间戳（`2026-09-12T22:22:00Z` / 带偏移均可）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc_naive(dt)


def _payload_dict(raw: str) -> dict[str, Any]:
    import json

    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _push_pusher(data: dict[str, Any]) -> str:
    """跨平台提取推送人：GitHub/Gitea `pusher.name|login`；GitLab `user_*`；Gitee 兜底。"""
    pusher = data.get("pusher")
    if isinstance(pusher, dict):
        for key in ("name", "login", "username"):
            if pusher.get(key):
                return str(pusher[key])
    for key in ("user_username", "user_name"):
        if data.get(key):
            return str(data[key])
    return ""


def _commit_author(commit: dict[str, Any], fallback: str) -> str:
    """单条 commit 的作者名：`author.name|username|login`，缺了退回推送人。"""
    author = commit.get("author")
    if isinstance(author, dict):
        for key in ("name", "username", "login"):
            if author.get(key):
                return str(author[key])
    if isinstance(author, str) and author:
        return author
    return fallback


def _events_from_row(
    event_type: str, pr_author: str, queued_at: datetime, payload: str
) -> list[_Event]:
    """一行 review_task → 提交事件列表（push 轨尽量展开到每条 commit）。"""
    if event_type != "push":
        return [] if not pr_author else []
    data = _payload_dict(payload)
    pusher = _push_pusher(data)
    raw_commits = data.get("commits")
    events: list[_Event] = []
    if isinstance(raw_commits, list) and raw_commits:
        for item in raw_commits:
            if not isinstance(item, dict):
                continue
            ts = _parse_iso(item.get("timestamp")) or queued_at
            events.append(_Event(
                author=_commit_author(item, pusher), ts=ts,
            ))
        return events
    # 无 commits（删分支 / 解析失败）：退化为「推送人在入队时间」一条
    return [_Event(author=pusher, ts=queued_at)] if pusher else []


def _local_shift(tz_minutes: int) -> timedelta:
    """JS `getTimezoneOffset()` 语义：东八区返回 -480 → 本地 = UTC - offset。"""
    return timedelta(minutes=-tz_minutes)


# ── 统计聚合 ────────────────────────────────────────────────────────────────


@dataclass
class _AuthorAgg:
    commits: int = 0
    dates: set[str] = field(default_factory=set)
    late_night: int = 0
    weekend: int = 0
    events: list[_Event] = field(default_factory=list)


def _longest_streak(dates: list[str]) -> tuple[int, str, str]:
    """最长连续天数（本地日期连续 +1）：返回 (天数, 起始, 结束)；空给 (0, "", "")。"""
    if not dates:
        return 0, "", ""
    parsed = sorted(datetime.strptime(d, "%Y-%m-%d") for d in dates)
    best_len = cur_len = 1
    best_start = cur_start = parsed[0]
    best_end = parsed[0]
    for prev, cur in zip(parsed, parsed[1:], strict=False):
        if (cur - prev).days == 1:
            cur_len += 1
        else:
            cur_len, cur_start = 1, cur
        if cur_len > best_len:
            best_len, best_start, best_end = cur_len, cur_start, cur
    fmt = "%Y-%m-%d"
    return best_len, best_start.strftime(fmt), best_end.strftime(fmt)


def _index_parts(
    *, late_pct: float, night_pct: float, weekend_pct: float,
    streak: int, daily_avg: float, attendance: float,
) -> list[dict[str, Any]]:
    """六维辛苦指数（满分 100）。分档基准（可整除百分位/天数，保持可解释）：

    - 深夜作战 25：深夜(0-6点)占比 20% 拉满
    - 夜间在线 15：夜间(23-6点)占比 35% 拉满
    - 周末在岗 15：周末占比 25% 拉满
    - 连续作战 20：最长连续 14 天拉满
    - 投入密度 15：活跃日均 8 次拉满
    - 出勤强度 10：活跃天数 / 跨度天数 70% 拉满
    """
    raw: list[tuple[str, float, float, float]] = [
        ("deep_night", late_pct, 20.0, 25.0),
        ("night", night_pct, 35.0, 15.0),
        ("weekend", weekend_pct, 25.0, 15.0),
        ("streak", float(streak), 14.0, 20.0),
        ("density", daily_avg, 8.0, 15.0),
        ("attendance", attendance, 70.0, 10.0),
    ]
    parts = []
    for code, value, basis, full in raw:
        parts.append({
            "code": code,
            "value": round(value, 1),
            "score": round(min(full, full * value / basis), 1) if basis else 0.0,
            "max": full,
        })
    return parts


def _level_of(score: float) -> str:
    """指数分档：<30 轻松节奏 / 30-55 稳定输出 / 55-75 重度拼搏 / ≥75 极限爆肝。"""
    if score < 30:
        return "relaxed"
    if score < 55:
        return "steady"
    if score < 75:
        return "intense"
    return "extreme"


def _pct(n: int, total: int) -> float:
    return round(n * 100.0 / total, 1) if total else 0.0


def _aggregate(
    events: list[_Event], tz_minutes: int
) -> dict[str, Any]:
    """纯聚合：事件 → KPI / 指数 / 三张分布 / 主战场 / 用户排行 / 洞察 / 建议。"""
    shift = _local_shift(tz_minutes)
    hourly = [0] * 24
    weekday = [0] * 7  # 0=周一 … 6=周日
    monthly: Counter[str] = Counter()
    dates: set[str] = set()
    repos: Counter[str] = Counter()
    repo_ids: Counter[str] = Counter()
    by_author: dict[str, _AuthorAgg] = {}

    late_night = night = non_work = weekend = night_or_weekend = 0
    for ev in events:
        local = ev.ts + shift
        hour = local.hour
        iso_day = local.isoweekday()  # 1=周一 … 7=周日
        day_str = local.date().isoformat()
        hourly[hour] += 1
        weekday[iso_day - 1] += 1
        monthly[local.strftime("%Y-%m")] += 1
        dates.add(day_str)
        repos[ev.repo_label] += 1
        repo_ids[str(ev.project_id)] += 1
        if hour in DEEP_NIGHT_HOURS:
            late_night += 1
        if hour in NIGHT_HOURS:
            night += 1
        if hour in NON_WORK_HOURS:
            non_work += 1
        if iso_day in WEEKEND_DAYS:
            weekend += 1
        if hour in NIGHT_HOURS or iso_day in WEEKEND_DAYS:
            night_or_weekend += 1
        agg = by_author.setdefault(ev.author, _AuthorAgg())
        agg.commits += 1
        agg.dates.add(day_str)
        agg.late_night += 1 if hour in DEEP_NIGHT_HOURS else 0
        agg.weekend += 1 if iso_day in WEEKEND_DAYS else 0
        agg.events.append(ev)

    total = len(events)
    active_days = len(dates)
    streak, streak_from, streak_to = _longest_streak(sorted(dates))
    daily_avg = round(total / active_days, 1) if active_days else 0.0
    span_days = 0
    if dates:
        all_days = sorted(datetime.strptime(d, "%Y-%m-%d") for d in dates)
        span_days = (all_days[-1] - all_days[0]).days + 1
    attendance = round(active_days * 100.0 / span_days, 1) if span_days else 0.0
    late_pct, night_pct, weekend_pct = (
        _pct(late_night, total), _pct(night, total), _pct(weekend, total),
    )
    parts = _index_parts(
        late_pct=late_pct, night_pct=night_pct, weekend_pct=weekend_pct,
        streak=streak, daily_avg=daily_avg, attendance=attendance,
    )
    score = round(min(100.0, sum(p["score"] for p in parts)), 1)

    authors = []
    for name, agg in by_author.items():
        a_late = _pct(agg.late_night, agg.commits)
        a_weekend = _pct(agg.weekend, agg.commits)
        a_avg = round(agg.commits / len(agg.dates), 1) if agg.dates else 0.0
        a_span = 0
        if agg.dates:
            ds = sorted(datetime.strptime(d, "%Y-%m-%d") for d in agg.dates)
            a_span = (ds[-1] - ds[0]).days + 1
        a_parts = _index_parts(
            late_pct=a_late, night_pct=_pct(
                sum(1 for e in agg.events
                    if (e.ts + shift).hour in NIGHT_HOURS), agg.commits),
            weekend_pct=a_weekend,
            streak=_longest_streak(sorted(agg.dates))[0],
            daily_avg=a_avg,
            attendance=round(len(agg.dates) * 100.0 / a_span, 1) if a_span else 0.0,
        )
        a_score = round(min(100.0, sum(p["score"] for p in a_parts)), 1)
        authors.append({
            "name": name,
            "commits": agg.commits,
            "active_days": len(agg.dates),
            "late_night": agg.late_night,
            "weekend": agg.weekend,
            "index": a_score,
            "level": _level_of(a_score),
        })
    authors.sort(key=lambda a: (-a["commits"], a["name"]))

    hourly_out = [
        {"hour": h, "count": hourly[h], "band": _hour_band(h)} for h in range(24)
    ]
    weekday_out = [{"day": d + 1, "count": weekday[d]} for d in range(7)]
    monthly_out = [
        {"month": m, "count": c} for m, c in sorted(monthly.items())
    ]
    top_repos = [
        {"name": name or "未知仓库", "count": cnt}
        for name, cnt in repos.most_common(5)
    ]

    insights, suggestions = _narrate(
        total=total, hourly=hourly, weekday=weekday, top_repos=top_repos,
        late_pct=late_pct, night_pct=night_pct, weekend_pct=weekend_pct,
        night_or_weekend_pct=_pct(night_or_weekend, total),
        streak=streak, streak_from=streak_from, streak_to=streak_to,
    )

    return {
        "kpi": {
            "total": total,
            "active_days": active_days,
            "daily_avg": daily_avg,
            "longest_streak": streak,
            "streak_from": streak_from,
            "streak_to": streak_to,
            "late_night": late_night,
            "late_night_pct": late_pct,
            "night": night,
            "night_pct": night_pct,
            "non_work": non_work,
            "non_work_pct": _pct(non_work, total),
            "weekend": weekend,
            "weekend_pct": weekend_pct,
            "night_or_weekend": night_or_weekend,
            "night_or_weekend_pct": _pct(night_or_weekend, total),
        },
        "index": {
            "score": score if total else 0.0,
            "level": _level_of(score) if total else "none",
            "parts": parts,
        },
        "hourly": hourly_out,
        "weekday": weekday_out,
        "monthly": monthly_out,
        "repos": top_repos,
        "authors": authors,
        "insights": insights,
        "suggestions": suggestions,
        "span_days": span_days,
        "from": min(dates) if dates else "",
        "to": max(dates) if dates else "",
    }


def _narrate(
    *, total: int, hourly: list[int], weekday: list[int], top_repos: list[dict[str, Any]],
    late_pct: float, night_pct: float, weekend_pct: float, night_or_weekend_pct: float,
    streak: int, streak_from: str, streak_to: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """确定性洞察/建议：返回 `{code, params}` 列表，文案由前端 i18n 渲染。"""
    insights: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    if total < 5:
        if total:
            insights.append({"code": "sample_small", "params": {"n": total}})
        return insights, suggestions

    # 峰值时段：第一名；若存在不同时段带的第二名 → 双高峰
    order = sorted(range(24), key=lambda h: (-hourly[h], h))
    h1 = order[0]
    band1 = _hour_band(h1)
    h2 = next((h for h in order[1:] if hourly[h] > 0 and _hour_band(h) != band1), None)
    if h2 is not None:
        insights.append({"code": "dual_peak", "params": {
            "h1": h1, "n1": hourly[h1], "h2": h2, "n2": hourly[h2],
        }})
    else:
        insights.append({"code": "single_peak", "params": {"h": h1, "n": hourly[h1]}})

    dawn = hourly[0] + hourly[1]
    if dawn:
        insights.append({"code": "night_owl", "params": {"n": dawn}})
    top_day = max(range(7), key=lambda d: (weekday[d], -d))
    if weekday[top_day]:
        insights.append({"code": "top_weekday", "params": {
            "day": top_day + 1, "n": weekday[top_day],
            "sat": weekday[5], "sun": weekday[6],
        }})
    if top_repos:
        insights.append({"code": "main_repos", "params": {
            "names": "、".join(r["name"] for r in top_repos[:3]),
        }})
    if streak >= 3:
        insights.append({"code": "streak_info", "params": {
            "days": streak, "from": streak_from, "to": streak_to,
        }})

    if late_pct >= 5:
        suggestions.append({"code": "late_shift", "params": {"pct": late_pct}})
    if streak >= 7:
        suggestions.append({"code": "streak_rest", "params": {"days": streak}})
    if night_or_weekend_pct >= 30:
        suggestions.append({"code": "rest_ratio", "params": {"pct": night_or_weekend_pct}})
    if not suggestions:
        suggestions.append({"code": "healthy", "params": {}})
    return insights, suggestions


# ── 数据装载 + 路由 ─────────────────────────────────────────────────────────


async def _load_events(
    session: AsyncSession,
    *,
    is_global: bool,
    ids: set[int],
    project_id: int = 0,
    days: int = DEFAULT_DAYS,
    author: str = "",
) -> list[_Event]:
    """按 RBAC 作用域 + 筛选装载提交事件（MR 直取行、push 拆 payload commits）。"""
    wheres: list[Any] = []
    scope = review_scope_clause(is_global, ids)
    if scope is not None:
        wheres.append(scope)
    if project_id:
        wheres.append(ReviewTask.project_id == project_id)
    if days:
        wheres.append(
            ReviewTask.queued_at >= datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
        )
    rows = (await session.execute(
        select(
            ReviewTask.event_type, ReviewTask.project_id, ReviewTask.pr_author,
            ReviewTask.queued_at, ReviewTask.payload,
            ReviewTask.provider, ReviewTask.repo_id,
        ).where(*wheres)
    )).all()

    # repo 展示名：存量行 project_id 可空，按 (provider, repo_id) 兜底映射
    proj_rows = (await session.execute(select(Project))).scalars().all()
    label_by_key = {f"{p.provider}:{p.repo_id}": (p.repo_full_name or p.repo_id)
                    for p in proj_rows}
    pid_by_key = {f"{p.provider}:{p.repo_id}": p.id for p in proj_rows}

    events: list[_Event] = []
    for event_type, row_pid, pr_author, queued_at, payload, provider, repo_id in rows:
        key = f"{provider}:{repo_id}"
        label = label_by_key.get(key, repo_id)
        pid = row_pid if row_pid is not None else pid_by_key.get(key)
        queued = _as_utc_naive(queued_at) if queued_at else None
        if queued is None:
            continue
        if event_type != "push":
            # MR 轨：作者缺失（历史/异常行）不进统计，避免聚成「无名」桶
            who = pr_author or ""
            if author and who != author:
                continue
            if who:
                events.append(_Event(author=who, ts=queued, project_id=pid, repo_label=label))
            continue
        for ev in _events_from_row(event_type, pr_author, queued, payload):
            if not ev.author:
                continue
            if author and ev.author != author:
                continue
            events.append(_Event(
                author=ev.author, ts=ev.ts, project_id=pid, repo_label=label,
            ))
    return events


def _norm_days(days: int) -> int:
    return days if days in ALLOWED_DAYS else DEFAULT_DAYS


def _norm_tz(tz: int) -> int:
    return max(-840, min(840, tz))


class WorkrateOptions(BaseModel):
    authors: list[dict[str, Any]]
    projects: list[dict[str, Any]]


class WorkrateReport(BaseModel):
    scope: dict[str, Any]
    kpi: dict[str, Any]
    index: dict[str, Any]
    hourly: list[dict[str, Any]]
    weekday: list[dict[str, Any]]
    monthly: list[dict[str, Any]]
    repos: list[dict[str, Any]]
    authors: list[dict[str, Any]]
    insights: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]


async def _scope_of(session: AsyncSession, user: CurrentUser) -> tuple[bool, set[int]]:
    return await allowed_project_ids(session, user)


@router.get("/options", response_model=WorkrateOptions)
async def get_workrate_options(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    project_id: int = 0,
    days: int = DEFAULT_DAYS,
) -> WorkrateOptions:
    """筛选下拉数据：作用域内出现过的作者与项目（随 project/days 联动，不含 author 过滤）。"""
    is_global, ids = await _scope_of(session, user)
    events = await _load_events(
        session, is_global=is_global, ids=ids,
        project_id=project_id, days=_norm_days(days),
    )
    by_author: Counter[str] = Counter(e.author for e in events)
    by_repo: dict[str, Counter[str]] = {}
    by_pid: Counter[str] = Counter()
    for e in events:
        key = str(e.project_id or 0)
        by_pid[key] += 1
        by_repo.setdefault(key, Counter())[e.repo_label] += 1
    projects = [
        {"id": int(k) if k and k != "0" else None, "count": n,
         "name": next(iter(by_repo[k].most_common(1)[0])) if by_repo.get(k) else ""}
        for k, n in by_pid.most_common()
    ]
    authors = [{"name": a, "count": c} for a, c in by_author.most_common()]
    return WorkrateOptions(authors=authors, projects=projects)


@router.get("/report", response_model=WorkrateReport)
async def get_workrate_report(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    project_id: int = 0,
    author: str = Query("", max_length=255),
    days: int = DEFAULT_DAYS,
    tz: int = 0,
) -> WorkrateReport:
    """提交时间 · 工作辛苦度报告：KPI + 六维指数 + 分布 + 排行 + 洞察建议。

    `tz` 为前端 `getTimezoneOffset()` 分钟数（东八区 -480）；所有本地时间分桶按其平移。
    """
    is_global, ids = await _scope_of(session, user)
    norm_days = _norm_days(days)
    events = await _load_events(
        session, is_global=is_global, ids=ids,
        project_id=project_id, days=norm_days, author=author.strip(),
    )
    body = _aggregate(events, _norm_tz(tz))
    distinct_projects = len({e.project_id for e in events if e.project_id is not None})
    body["scope"] = {
        "project_id": project_id,
        "author": author.strip(),
        "days": norm_days,
        "commits": len(events),
        "projects_count": distinct_projects,
        "from": body.pop("from", ""),
        "to": body.pop("to", ""),
    }
    return WorkrateReport(**body)

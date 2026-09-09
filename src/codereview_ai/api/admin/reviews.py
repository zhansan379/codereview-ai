"""审查记录 REST（DESIGN §14.2）：列表 + 详情（含 findings 汇总）。

服务端分页（limit/offset），禁止把全表 SELECT 进前端（DESIGN §14.1）；可选按 state 过滤。
"""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from starlette.responses import StreamingResponse

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.review.compare import bucket_compare
from codereview_ai.review.pr_compare import group_pr_deltas
from codereview_ai.storage.models import ReviewConversation, ReviewFinding, ReviewTask

router = APIRouter(prefix="/reviews", dependencies=[Depends(get_current_user)])

_StmtT = TypeVar("_StmtT", bound=tuple[object, ...])

# 评审记录列表 / 导出具用的顶层筛选（DESIGN §14.1 服务端过滤）。返回过滤后的语句。
def _apply_review_filters(
    stmt: Select[_StmtT],
    state: str | None = None,
    event_type: str | None = None,
    provider: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    finished_from: datetime | None = None,
    finished_to: datetime | None = None,
) -> Select[_StmtT]:
    if state:
        stmt = stmt.where(ReviewTask.state == state)
    if event_type:
        stmt = stmt.where(ReviewTask.event_type == event_type)
    if provider:
        stmt = stmt.where(ReviewTask.provider == provider)
    if score_min is not None:
        stmt = stmt.where(ReviewTask.score_total >= score_min)
    if score_max is not None:
        stmt = stmt.where(ReviewTask.score_total <= score_max)
    if finished_from is not None:
        stmt = stmt.where(ReviewTask.finished_at >= finished_from)
    if finished_to is not None:
        stmt = stmt.where(ReviewTask.finished_at <= finished_to)
    return stmt


class ReviewListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    repo_id: str
    pr_number: int | None
    pr_title: str
    web_url: str = ""  # mr: MR/PR 页 URL；push: {仓库}/commit/{head_sha}
    push_commits: str = ""
    event_type: str
    branch: str
    head_sha: str
    base_sha: str
    state: str
    attempt: int
    error: str
    skip_reason: str
    trace_id: str
    summary_md: str
    score_total: int
    queued_at: datetime
    finished_at: datetime | None
    writeback_failed: bool
    diff_lines: int = 0  # 新增+删除行合计（复杂度度量）
    exec_mode: str | None = None  # 实际执行模式（agentic / diff）；NULL=未执行审查


class ReviewDetail(ReviewListItem):
    findings: list[ReviewFindingOut] = []
    # 同一 MR 上一次 completed 审查任务 id；None = 首轮（无「上次」可对比）。
    prev_round_id: int | None = None


class FindingStatusUpdate(BaseModel):
    status: str


class ReviewFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    severity: str
    category: str
    file: str
    old_line: int | None
    new_line: int | None
    title: str
    detail: str
    existing_code: str
    suggestion: str
    source: str
    status: str
    first_seen: datetime
    last_seen: datetime
    reopened_count: int


class ReviewPage(BaseModel):
    items: list[ReviewListItem]
    total: int
    limit: int
    offset: int


class PrRoundOut(BaseModel):
    """一轮已完成审查的收敛情况（计数，供 PR 时间线渲染）。"""

    id: int
    head_sha: str
    delta: dict[str, int]


class ReviewPr(BaseModel):
    """一个 MR 的聚合收敛视图（多轮已完成审查串成时间线 + 末轮四桶）。"""

    key: str
    provider: str
    repo_id: str
    pr_number: int
    pr_title: str
    web_url: str = ""
    branch: str = ""
    rounds_count: int
    rounds: list[PrRoundOut]
    last_delta: dict[str, list[dict[str, object]]]
    rate_pct: int


class ReviewPrPage(BaseModel):
    items: list[ReviewPr]
    total: int
    limit: int
    offset: int


@router.get("", response_model=ReviewPage)
async def list_reviews(
    session: AsyncSession = Depends(get_db),
    state: str | None = None,
    event_type: str | None = None,
    provider: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    finished_from: datetime | None = None,
    finished_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ReviewPage:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    stmt = _apply_review_filters(
        select(ReviewTask),
        state=state, event_type=event_type, provider=provider,
        score_min=score_min, score_max=score_max,
        finished_from=finished_from, finished_to=finished_to,
    )
    count_stmt = _apply_review_filters(select(ReviewTask.id), state=state,
                                       event_type=event_type, provider=provider,
                                       score_min=score_min, score_max=score_max,
                                       finished_from=finished_from, finished_to=finished_to)
    total = (await session.execute(
        select(func.count()).select_from(count_stmt.subquery())
    )).scalar_one()
    rows = (await session.execute(
        stmt.order_by(ReviewTask.id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    items = [ReviewListItem.model_validate(r) for r in rows]
    return ReviewPage(items=items, total=total, limit=limit, offset=offset)


# 严重度/状态/来源 → 中文标签（导出 Excel 用，与前端展示一致）。
_SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "error": "错误",
    "medium": "中",
    "warning": "警告",
    "low": "低",
    "info": "提示",
}
_STATUS_LABELS = {
    "active": "待处理",
    "resolved": "已解决",
    "waived": "已搁置",
}
_SOURCE_LABELS = {"llm": "LLM", "static": "静态分析"}

# Excel 列定义：(表头, 取值回调)。回调入参为 (review, finding) 元组。
_EXPORT_HEADERS: list[tuple[str, str]] = [
    ("评审ID", "review_id"),
    ("平台", "provider"),
    ("仓库", "repo_id"),
    ("PR号", "pr_number"),
    ("事件类型", "event_type"),
    ("分支", "branch"),
    ("严重度", "severity"),
    ("类别", "category"),
    ("文件", "file"),
    ("行号", "new_line"),
    ("来源", "source"),
    ("状态", "status"),
    ("问题标题", "title"),
    ("详细分析", "detail"),
    ("原代码", "existing_code"),
    ("建议修复", "suggestion"),
]


def _label(value: str | None, mapping: dict[str, str]) -> str:
    if not value:
        return ""
    return mapping.get(value, value)


def _build_export_xlsx(rows: list[tuple[ReviewTask, ReviewFinding]]) -> BytesIO:
    """把 (task, finding) 行写入 .xlsx 内存流。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "评审问题"
    ws.append([label for label, _key in _EXPORT_HEADERS])

    # 表头样式：加粗 + 灰底 + 居中；冻结首行。
    header_fill = PatternFill("solid", fgColor="D9E1F2")
    header_font = Font(bold=True)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for review, finding in rows:
        ws.append([_cell_value(_key, review, finding) for _, _key in _EXPORT_HEADERS])

    # 文本列开启自动换行并设定列宽，避免「详细分析/源码」被截断。
    for idx, (_, key) in enumerate(_EXPORT_HEADERS, start=1):
        letter = get_column_letter(idx)
        width = 60 if key in {"detail", "existing_code", "suggestion", "title"} else 16
        ws.column_dimensions[letter].width = width
        ws[f"{letter}1"].alignment = Alignment(vertical="center")
        for cell in ws[letter][1:]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _cell_value(key: str, review: ReviewTask, finding: ReviewFinding) -> object:
    if key == "severity":
        return _label(finding.severity, _SEVERITY_LABELS)
    if key == "status":
        return _label(finding.status, _STATUS_LABELS)
    if key == "source":
        if finding.source and finding.source.startswith("static"):
            return "静态分析"
        return _label(finding.source, _SOURCE_LABELS)
    if key in {"new_line", "old_line"}:
        value = getattr(finding, key)
        return "" if value is None else value
    # 其余列直接读取 finding 字段（保留协商列名）；findings 没有则回退到 review。
    if hasattr(finding, key):
        value = getattr(finding, key)
        return "" if value is None else value
    return getattr(review, key, "")


@router.get("/export")
async def export_reviews(
    session: AsyncSession = Depends(get_db),
    state: str | None = None,
    event_type: str | None = None,
    provider: str | None = None,
    score_min: int | None = None,
    score_max: int | None = None,
    finished_from: datetime | None = None,
    finished_to: datetime | None = None,
    severities: list[str] | None = Query(None),
    statuses: list[str] | None = Query(None),
) -> StreamingResponse:
    """导出筛选结果的全部评审问题明细为 .xlsx（不受分页限制）。

    `severities` / `statuses` 用于过滤要导出的问题条目；其余顶层筛选与列表一致，
    保证「所见即所导」。沿用 get_current_user 鉴权（router 级依赖）。
    """
    stmt = _apply_review_filters(
        select(ReviewTask, ReviewFinding)
        .join(ReviewFinding, ReviewFinding.task_id == ReviewTask.id)
        .order_by(ReviewTask.id.desc(), ReviewFinding.id),
        state=state, event_type=event_type, provider=provider,
        score_min=score_min, score_max=score_max,
        finished_from=finished_from, finished_to=finished_to,
    )
    if severities:
        stmt = stmt.where(ReviewFinding.severity.in_(severities))
    if statuses:
        stmt = stmt.where(ReviewFinding.status.in_(statuses))

    rows: list[tuple[ReviewTask, ReviewFinding]] = [
        (review, finding) for review, finding in (await session.execute(stmt)).all()
    ]
    buf = _build_export_xlsx(rows)

    filename = datetime.now().strftime("review_issues_%Y%m%d_%H%M%S.xlsx")
    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/prs", response_model=ReviewPrPage)
async def list_review_prs(
    session: AsyncSession = Depends(get_db),
    provider: str | None = None,
    pr_number: int | None = None,
    q: str | None = None,
    finished_from: datetime | None = None,
    finished_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ReviewPrPage:
    """按 MR 聚合的收敛视图：同一 MR（provider+repo+pr_number）多轮已完成审查，
    把相邻轮 findings 差量串成时间线 + 末轮四桶（`group_pr_deltas` 纯函数）。

    注意：本路由必须注册在 `/{review_id}` 之前，否则 `/prs` 会被
    `{review_id:int}` 抢先匹配而解析失败（422）。

    - 只统计 `event_type=='mr' and state=='completed'` 的任务；首轮 `before=[]` 全进 new。
    - 筛选：`provider` 平台 / `pr_number` MR 号精确 / `q` 标题关键词 / 时间范围（按任务时间）。
    - PR 之间按最近一轮 id 降序分页（`limit`/`offset`）。
    - findings 一次取全（非 N+1），再按 task_id 归组喂给纯函数。
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    kw = (q or "").strip().lower()
    tasks = list((await session.execute(
        select(ReviewTask)
        .where(
            ReviewTask.event_type == "mr",
            ReviewTask.state == "completed",
            ReviewTask.pr_number.is_not(None),
        )
        .order_by(ReviewTask.id)  # 本 PR 内按 id 升序 ≡ 审查顺序
    )).scalars().all())
    tasks = [t for t in tasks
             if (not provider or t.provider == provider)
             and (pr_number is None or t.pr_number == pr_number)
             and (not kw or (t.pr_title or "").lower().find(kw) >= 0)
             and (not finished_from or (t.finished_at or t.queued_at) >= finished_from)
             and (not finished_to or (t.finished_at or t.queued_at) <= finished_to)]

    # findings 一次拉全，按 task_id 归组（避免逐任务 N+1）。
    findings_by_task: dict[int, list[_FindRow]] = {}
    if tasks:
        rows = (await session.execute(
            select(ReviewFinding).where(ReviewFinding.task_id.in_(
                [t.id for t in tasks]
            )).order_by(ReviewFinding.id)
        )).scalars().all()
        for r in rows:
            findings_by_task.setdefault(r.task_id, []).append(_FindRow(r))

    # 按 (provider, repo_id, pr_number) 分组；组内已按 task.id 升序。
    grouped: dict[tuple[str, str, int], list[tuple[int, str, list[_FindRow], set[str]]]] = {}
    for t in tasks:
        grouped.setdefault((t.provider, t.repo_id, t.pr_number), []).append(
            (t.id, t.head_sha, findings_by_task.get(t.id, []), _covered_paths(t))
        )

    prs = group_pr_deltas(grouped)
    prs.sort(key=lambda p: p.rounds[-1].id if p.rounds else 0, reverse=True)

    items: list[ReviewPr] = []
    for p in prs[offset:offset + limit]:
        t = next(t for t in tasks if f"{t.provider}:{t.repo_id}:{t.pr_number}" == p.key)
        items.append(ReviewPr(
            key=p.key, provider=t.provider, repo_id=t.repo_id,
            pr_number=t.pr_number or 0, pr_title=t.pr_title, web_url=t.web_url,
            branch=t.branch, rounds_count=len(p.rounds),
            rounds=[PrRoundOut(id=r.id, head_sha=r.head_sha, delta=r.delta)
                    for r in p.rounds],
            last_delta=p.last_delta, rate_pct=p.rate_pct,
        ))
    return ReviewPrPage(items=items, total=len(prs), limit=limit, offset=offset)


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(review_id: int, session: AsyncSession = Depends(get_db)) -> ReviewDetail:
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == review_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审查记录不存在")
    findings = (await session.execute(
        select(ReviewFinding).where(ReviewFinding.task_id == review_id).order_by(
            ReviewFinding.id
        )
    )).scalars().all()
    detail = ReviewDetail(**ReviewListItem.model_validate(row).model_dump())
    detail.findings = [ReviewFindingOut.model_validate(f) for f in findings]
    # 首轮判定：找同一 MR 上一次 completed 任务（口径与 /compare 一致）。
    if row.event_type == "mr":
        prev = (await session.execute(
            select(ReviewTask.id).where(
                ReviewTask.provider == row.provider,
                ReviewTask.repo_id == row.repo_id,
                ReviewTask.pr_number == row.pr_number,
                ReviewTask.event_type == "mr",
                ReviewTask.state == "completed",
                ReviewTask.id != review_id,
            ).order_by(ReviewTask.id.desc()).limit(1)
        )).scalar_one_or_none()
        detail.prev_round_id = prev
    return detail


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(review_id: int, session: AsyncSession = Depends(get_db)) -> None:
    """删除一条审查记录（含其 findings，级联）。管理员清理脏数据用。

    `review_finding.task_id` 为 `ondelete="CASCADE"`（SQLite 已开 foreign_keys），
    删 task 时 finding 自动级联。注意：删 `completed` 行会移除其「下次增量基线/
    指标统计」锚点，同 head 可能被重新全量审查（预期副作用的直删实现）。
    """
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == review_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审查记录不存在")
    await session.delete(row)
    await session.commit()


def _truncate_inplace(value: object, cap: int = 4000) -> object:
    """把 request/response 里任意深度**超过 cap 的字符串**就地压到头部 + 截断标记。

    诊断页只需头部即可定位问题；源数据是每次 load 出来的独占 dict，就地改不共享。
    体积从整条多 MB 压到几十 KB，SQLite 读取 / json.loads / JSON 编码三条链路同时下降。
    """
    if isinstance(value, str):
        if len(value) > cap:
            return value[:cap] + f"…(truncated, {len(value)}B)"
        return value
    if isinstance(value, list):
        for i, v in enumerate(value):
            value[i] = _truncate_inplace(v, cap)
        return value
    if isinstance(value, dict):
        for k, v in list(value.items()):
            value[k] = _truncate_inplace(v, cap)
        return value
    return value


async def _task_or_404(review_id: int, session: AsyncSession) -> ReviewTask:
    """取审查任务行，不存在则抛 404（conversation/compare 共用）。"""
    row = (await session.execute(
        select(ReviewTask).where(ReviewTask.id == review_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "审查记录不存在")
    return row


@router.get("/{review_id}/conversation")
async def get_conversation(
    review_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """该审查任务逐条即时落库的原始 LLM 对话时间线（review_conversation 表）。

    agentic 多轮（plan/main/re_location/review_filter/scoring/compress）每轮
    `llm.chat()/summarize()` 一条；按插入序（id ASC）返回真实时序。diff 轨不采集，
    该任务对话为空时就返回空 items（前端据此隐藏入口）。

    性能（诊断页，体积驱动）：
    - 分页：`offset`/`limit` 控制返回窗口，`has_more` 是否还有后续，前端懒加载「加载更多」。
    - 直接返回纯 dict（不走 `response_model`），避免 Pydantic 二次构造 + 二次 JSON 编码；
      `ts` 预编码 ISO 规避 jsonable_encoder 逐值探测。
    - `_truncate_inplace` 把超大 content 压到头部，读取/loads/编码同步下降。
    """
    await _task_or_404(review_id, session)
    total = (await session.execute(
        select(func.count()).select_from(ReviewConversation)
        .where(ReviewConversation.task_id == review_id)
    )).scalar_one()
    rows = (await session.execute(
        select(ReviewConversation)
        .where(ReviewConversation.task_id == review_id)
        .order_by(ReviewConversation.id)
        .offset(offset)
        .limit(limit)
    )).scalars().all()
    items: list[dict[str, object]] = []
    for r in rows:
        req = r.request_json
        resp = r.response_json
        try:
            req_obj = json.loads(req) if req else None
        except json.JSONDecodeError:
            req_obj = req
        try:
            resp_obj = json.loads(resp) if resp else None
        except json.JSONDecodeError:
            resp_obj = resp
        items.append({
            "seq": r.seq, "phase": r.phase, "model": r.model, "trace_id": r.trace_id,
            "file_group": r.file_group or "",
            "request": _truncate_inplace(req_obj),
            "response": _truncate_inplace(resp_obj),
            "ts": r.ts.isoformat(),
        })
    return {
        "items": items, "offset": offset, "limit": limit,
        "total": total, "has_more": offset + len(rows) < total,
    }


class _FindRow:
    """把 ReviewFinding ORM 行适配成 `bucket_compare`/`finding_fingerprint` 需要的形态。

    compare 只读**原始快照字段**（file/detail/severity/category/new_line/…），
    不碰被 `reconcile_findings` 状态机改过的 `status`。content 取 `detail`（入库时的
    `Finding.content`），保证前后双侧指纹口径一致。
    """

    __slots__ = ("file", "content", "category", "severity", "line", "old_line",
                 "existing_code", "source")

    def __init__(self, r: ReviewFinding) -> None:
        self.file = r.file
        self.content = r.detail
        self.category = r.category
        self.severity = r.severity
        self.line = r.new_line
        self.old_line = r.old_line
        self.existing_code = r.existing_code
        self.source = r.source


def _covered_paths(task: ReviewTask) -> set[str]:
    """该任务本轮真正审到的文件路径集（来自 `diff_snapshot` 的 covered_file_map）。

    未变更文件复用后 diff_snapshot 只含**真正审到**的文件；存量老任务无快照 → 空集
    （保守：上次有本次无的进 `not_reviewed`，不算已修）。仅 mr 任务才有快照。
    """
    if not (task.diff_snapshot and task.event_type == "mr"):
        return set()
    try:
        snap = json.loads(task.diff_snapshot)
    except json.JSONDecodeError:
        return set()
    if not isinstance(snap, dict):
        return set()
    return {str(k) for k in snap}


@router.get("/{review_id}/compare")
async def compare_review(
    review_id: int, session: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    """该审查任务与**上一次 completed mr 任务**的 findings 四桶增量对比。

    桶语义（OCR `Compare`）：`new` 本次多出 / `persisting` 前后都有 /
    `resolved` 上次有、本次覆盖到已修 / `not_reviewed` 上次有但本次没碰（不算已修）。
    覆盖集读 `diff_snapshot`（未变更文件复用后只含真正审到的文件），存量老任务无
    `diff_snapshot` → 覆盖集为空 → 上次有本次无的进 `not_reviewed`（保守）。无上一次
    completed 任务 → 四桶全空。
    """
    current = await _task_or_404(review_id, session)
    after_rows = (await session.execute(
        select(ReviewFinding).where(ReviewFinding.task_id == review_id).order_by(ReviewFinding.id)  # noqa: E501
    )).scalars().all()
    # 本次覆盖集：diff_snapshot（covered_file_map 写入的 {path: sha1}）的路径集合。
    after_covered = _covered_paths(current)
    prev = (await session.execute(
        select(ReviewTask).where(
            ReviewTask.provider == current.provider,
            ReviewTask.repo_id == current.repo_id,
            ReviewTask.pr_number == current.pr_number,
            ReviewTask.event_type == "mr",
            ReviewTask.state == "completed",
            ReviewTask.id != review_id,
        ).order_by(ReviewTask.id.desc()).limit(1)
    )).scalar_one_or_none()
    before_rows: list[ReviewFinding] = []
    if prev is not None:
        before_rows = list((await session.execute(
            select(ReviewFinding).where(ReviewFinding.task_id == prev.id).order_by(ReviewFinding.id)  # noqa: E501
        )).scalars().all())

    result = bucket_compare(
        [_FindRow(b) for b in before_rows],
        [_FindRow(a) for a in after_rows],
        after_covered,
    )
    return {
        "new": result.new,
        "persisting": result.persisting,
        "resolved": result.resolved,
        "not_reviewed": result.not_reviewed,
    }


@router.post("/findings/{finding_id}/status", response_model=ReviewFindingOut)
async def set_finding_status(
    finding_id: int,
    body: FindingStatusUpdate,
    session: AsyncSession = Depends(get_db),
) -> ReviewFinding:
    """人工更新 finding 状态（DESIGN §7.3）：`waived`（搁置/忽略）↔ `active`（恢复）。

    `resolved` 由 worker 非增量全量对账自动托管，禁止手改（防误判已解决埋 bug）。
    """
    if body.status not in {"waived", "active"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "仅支持 waived/active；resolved 由系统对账托管，禁止手改",
        )
    from codereview_ai.storage.models import _utcnow

    row = (await session.execute(
        select(ReviewFinding).where(ReviewFinding.id == finding_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding 不存在")
    row.status = body.status
    row.last_seen = _utcnow()
    await session.commit()
    await session.refresh(row)
    return row

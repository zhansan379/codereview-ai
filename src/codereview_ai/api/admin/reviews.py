"""审查记录 REST（DESIGN §14.2）：列表 + 详情（含 findings 汇总）。

服务端分页（limit/offset），禁止把全表 SELECT 进前端（DESIGN §14.1）；可选按 state 过滤。
"""

from __future__ import annotations

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
from codereview_ai.storage.models import ReviewFinding, ReviewTask

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


class ReviewDetail(ReviewListItem):
    findings: list[ReviewFindingOut] = []


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

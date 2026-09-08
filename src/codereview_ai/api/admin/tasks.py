"""任务监控 REST（DESIGN §14.2）：任务列表 + 手动重试。

`POST /tasks/{id}/retry` 把 failed（或可补审的 push skipped）重置为 queued 并 attempt+1
（DESIGN §9.2），便于后台一键重新入队/补审。failed 与门控/配置类 skipped 可重试，
删分支（branch_deleted）等其余状态 409。push 轨重试会置 force_rerun 以绕过幂等预检。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.domain.models import PullRequest
from codereview_ai.forges.base import ForgeAdapter, repo_path_from_url
from codereview_ai.review.result_writer import redeliver, review_fingerprint
from codereview_ai.storage.models import ReviewTask, _utcnow
from codereview_ai.storage.review_repo import ReviewRepository

logger = logging.getLogger("codereview_ai.api.tasks")

router = APIRouter(prefix="/tasks", dependencies=[Depends(get_current_user)])


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    repo_id: str
    pr_number: int | None
    event_type: str
    branch: str
    head_sha: str
    state: str
    attempt: int
    error: str
    skip_reason: str
    queued_at: datetime
    #: 回写失败标记（DESIGN §9.2）：首次评论回写失败置 True，供前端展示「重新发送」。
    writeback_failed: bool


class TaskRetried(BaseModel):
    id: int
    state: str
    attempt: int


class TaskRedelivered(BaseModel):
    id: int
    status: str


async def _get_or_404(session: AsyncSession, task_id: int) -> ReviewTask:
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == task_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return row


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    session: AsyncSession = Depends(get_db), state: str | None = None
) -> list[TaskOut]:
    stmt = select(ReviewTask)
    if state:
        stmt = stmt.where(ReviewTask.state == state)
    rows = (await session.execute(stmt.order_by(ReviewTask.id.desc()))).scalars().all()
    return [TaskOut.model_validate(r) for r in rows]


#: 只要「门控/配置类」跳过的可补审；删分支（branch_deleted）无 head 可审，禁止
_RETRYABLE_SKIPPED_REASONS = {"push_disabled", "branch_mismatch"}


def _pr_from_task(row: ReviewTask) -> PullRequest:
    """从审计行重建一个中立 PR，供补拉同款 fetch 路径重放。

    补拉/补审入队的 MR 任务 `payload` 为空（原始 webhook body 不存在），重试无法用
    `enqueue` 重放原始事件。这里用行内字段重建一个 `PullRequest`，交给 `enqueue_pr`
    （补拉通道），worker 消费时走 `review_pull_request` 从平台实时 fetch——不再依赖 body。
    """
    return PullRequest(
        provider=row.provider,
        repo_id=row.repo_id,
        repo_full_name=(repo_path_from_url(row.web_url, row.provider) if row.web_url else ""),
        web_url=row.web_url,
        pr_number=row.pr_number or 0,
        title=row.pr_title,
        source_branch=row.branch,
        target_branch="",  # 审计行不落 target；fetch_pull_request 会补 diff_refs，target 仅展示用
        head_sha=row.head_sha,
        base_sha=row.base_sha,
    )


def _retryable(row: ReviewTask) -> bool:
    """该行是否允许手动重试/补审。

    - `failed` → 照旧可重试；
    - `skipped` + push 轨 + 门控/配置类原因 → 可补审（前端「补审」，绕过门控强审）；
    - 其余（queued/running/completed、branch_deleted、mr 轨 skipped）→ 不可。
    """
    if row.state == "failed":
        return True
    return bool(
        row.state == "skipped"
        and row.event_type == "push"
        and row.skip_reason in _RETRYABLE_SKIPPED_REASONS
    )


@router.post("/{task_id}/retry", response_model=TaskRetried)
async def retry_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskRetried:
    row = await _get_or_404(session, task_id)
    if not _retryable(row):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"该任务当前状态不可重试（{row.state}）",
        )
    row.state = "queued"
    row.attempt += 1
    row.error = ""
    row.skip_reason = ""
    row.queued_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    # simple 档队列：仅翻 DB 侧 queued 不会让内存 worker 重新拾取。持原事件且
    # enqueuer 就绪时把任务重新投进队列，worker 才会真去跑；否则退化为只记状态翻转。
    enqueuer = getattr(request.app.state, "enqueuer", None)
    re_enqueued = False
    if enqueuer is not None:
        if row.payload:
            # push 轨重跑会被幂等预检/门控短路；置 force_rerun 由 worker 强制补审（DESIGN §7.7）。
            # 仅 push 任务设，避免在 mr 行遗留无意义标记。
            if row.event_type == "push":
                row.force_rerun = True
                await session.commit()
            await enqueuer.enqueue(row.provider, row.payload.encode())
            re_enqueued = True
        elif row.event_type == "mr" and row.pr_number is not None:
            # 无原始 body（补拉/补审入队，payload 为空）：重建 PR 走补拉 fetch 路径再审。
            # 否则任务只翻 queued、内存队列里没有它，会永卡「排队中」。
            await enqueuer.enqueue_pr(row.provider, _pr_from_task(row))
            re_enqueued = True
        else:
            logger.warning("重试任务 %s：无 payload 且非 mr，无法重放（provider=%s, event=%s）",
                           row.id, row.provider, row.event_type)
        if re_enqueued:
            logger.info("重试任务 %s：已重新入队（provider=%s）", row.id, row.provider)
    return TaskRetried(id=row.id, state=row.state, attempt=row.attempt)


async def _background_redeliver(
    row: ReviewTask,
    forge: ForgeAdapter,
    repo: ReviewRepository,
) -> None:
    """后台重发已持久化的审查成果；成功归零 writeback_failed，失败保留标记供再点。

    整个重发**不做**任何 LLM 调用（用落库的 findings + summary_md），只重取 diff +
    回写；`redeliver` 内部自带指纹幂等 + 网络重试，重复点击不会在平台上双发。
    """
    pr = _pr_from_task(row)
    try:
        findings = await repo.findings_for_task(int(row.id))
        fp = review_fingerprint(
            provider=row.provider, repo_id=row.repo_id,
            pr_number=row.pr_number, head_sha=row.head_sha,
        )
        await redeliver(
            forge, pr=pr, findings=findings,
            summary_md=row.summary_md or "", fingerprint=fp,
        )
        await repo.mark_writeback(int(row.id), False)
        logger.info("重发成功：任务 %s 评论已补齐（provider=%s）", row.id, row.provider)
    except Exception as exc:  # noqa: BLE001
        # 失败仅记日志并保留 writeback_failed 标记：前端刷新后「重新发送」按钮仍在
        logger.warning("重发失败（任务 %s，provider=%s）：%s", row.id, row.provider, exc)


@router.post("/{task_id}/redeliver", response_model=TaskRedelivered)
async def redeliver_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TaskRedelivered:
    """重发已持久化的审查成果（不重算、无 LLM）。仅 `writeback_failed=True` 的任务可重发。

    首次回写失败后成果已在 DB（先落库方案）；这里从 DB 取 findings + summary_md，
    后台 fire-and-forget 走与正常回写同一套 `redeliver`（含指纹幂等）。立即返回
    「已发起」，成功与否在后台完成后翻转 `writeback_failed`，前端刷新可见。
    """
    row = await _get_or_404(session, task_id)
    if not row.writeback_failed:
        raise HTTPException(status.HTTP_409_CONFLICT, "该任务无需重发（writeback_failed 未置位）")
    if row.event_type != "mr" or row.pr_number is None:
        # 重发只针对 mr 轨（评论挂 PR）；push 轨评论挂 commit，走重试而不是重发
        raise HTTPException(status.HTTP_409_CONFLICT, "仅 mr 轨任务可重发评论")
    forge = request.app.state.forge_registry.get(row.provider)
    if forge is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"平台适配器不可用（{row.provider}）")
    # 与 settings.py 同模式：由 app.state.engine 现建仓储（DB 会话按调用自开）
    repo = ReviewRepository(request.app.state.engine)
    asyncio.create_task(_background_redeliver(row, forge, repo))
    logger.info("已发起重发：任务 %s（provider=%s）", row.id, row.provider)
    return TaskRedelivered(id=row.id, status="redelivering")

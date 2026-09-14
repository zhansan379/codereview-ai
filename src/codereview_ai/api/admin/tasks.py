"""任务监控 REST（DESIGN §14.2）：任务列表 + 手动重试/停止 + 批量操作。

- `POST /tasks/{id}/retry` 把 failed / skipped（未开始）重置为 queued 并 attempt+1
  （DESIGN §9.2）。failed=审过但出错重跑；skipped=未开始（门控跳过/未配置 LLM/手动
  停止/分支已删）手动执行——置 force_rerun 绕过门控与幂等预检强制审一次。
- `POST /tasks/{id}/stop` 把 queued 行翻成未开始（skipped/manual_stop）：内存队列里的
  残留项被 worker 认领后经 `ensure_task` 的未开始不可抢占语义自动 no-op，无需动队列。
- `POST /tasks/batch-execute` / `batch-stop`：审查记录页多选批量执行/停止，逐行复用
  单条逻辑，逐行返回状态（executed/ignored/denied），部分失败不中断其余。
- `POST /tasks/{id}/redeliver`：回写失败的任务重发已落库的评论（不重算）；retry/stop
  同款权限（可见范围 + `reviews:manage`）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette import status

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_review_scope,
    get_current_user,
    get_db,
    review_scope_clause,
    review_task_allowed,
    review_task_project_id,
    user_can,
)
from codereview_ai.domain.models import PullRequest
from codereview_ai.forges.base import ForgeAdapter, repo_path_from_url
from codereview_ai.ops.bootstrap import ensure_worker_started
from codereview_ai.review.result_writer import redeliver, review_fingerprint
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ReviewTask, _utcnow
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.storage.system_notification_repo import SystemNotificationRepository

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


class TaskStopped(BaseModel):
    id: int
    state: str
    skip_reason: str


class BatchIdsRequest(BaseModel):
    ids: list[int]


class BatchItemResult(BaseModel):
    id: int
    #: executed=已执行；ignored=状态不符跳过；denied=无权限/不存在
    status: str


class BatchResult(BaseModel):
    executed: int
    ignored: int
    denied: int
    results: list[BatchItemResult]


async def _get_or_404(session: AsyncSession, task_id: int) -> ReviewTask:
    row = (await session.execute(select(ReviewTask).where(ReviewTask.id == task_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return row


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    state: str | None = None,
) -> list[TaskOut]:
    stmt = select(ReviewTask)
    if state:
        stmt = stmt.where(ReviewTask.state == state)
    is_global, ids = await allowed_review_scope(session, user)
    scope_clause = review_scope_clause(is_global, ids)
    if scope_clause is not None:
        stmt = stmt.where(scope_clause)
    rows = (await session.execute(stmt.order_by(ReviewTask.id.desc()))).scalars().all()
    return [TaskOut.model_validate(r) for r in rows]


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
        target_branch=row.target_branch or "",
        author=row.pr_author or "",
        head_sha=row.head_sha,
        base_sha=row.base_sha,
    )


def _retryable(row: ReviewTask) -> bool:
    """该行是否允许手动重试/执行。

    - `failed` → 重试（审过但出错，重跑一次）；
    - `skipped`（未开始）→ 执行：门控跳过（绕过开关强制审一次）、未配置 LLM、手动停止、
      分支已删（push 轨全零 after）均放行——最后一类执行会自然失败转 failed，用户能在
      error 里看到明确原因，好过按钮永远点不了；
    - 其余（queued/running/completed）→ 不可。
    """
    return row.state in ("failed", "skipped")


async def _requeue_row(request: Request, session: AsyncSession, row: ReviewTask) -> None:
    """把一行翻回 queued 并重新投进内存队列。

    simple 档队列：仅翻 DB 侧 queued 不会让内存 worker 重新拾取。持原事件且
    enqueuer 就绪时把任务重新投进队列，worker 才会真去跑；否则退化为只记状态翻转。
    置 force_rerun：push/mr 轨重跑都会被门控短路，由 worker 强制补审并消费清除
    （DESIGN §7.7）。
    """
    row.state = "queued"
    row.attempt += 1
    row.error = ""
    row.skip_reason = ""
    row.queued_at = _utcnow()
    await session.commit()
    await session.refresh(row)
    enqueuer = getattr(request.app.state, "enqueuer", None)
    re_enqueued = False
    if enqueuer is not None:
        if row.payload:
            row.force_rerun = True
            await session.commit()
            await enqueuer.enqueue(row.provider, row.payload.encode())
            re_enqueued = True
        elif row.event_type == "mr" and row.pr_number is not None:
            # 无原始 body（补拉/补审入队，payload 为空）：重建 PR 走补拉 fetch 路径再审；
            # 同样置 force_rerun，使 worker 的 mr 门控放行这次手动补审。否则任务只翻 queued、
            # 内存队列里没有它，会永卡「排队中」。
            row.force_rerun = True
            await session.commit()
            await enqueuer.enqueue_pr(row.provider, _pr_from_task(row))
            re_enqueued = True
        else:
            logger.warning("重试任务 %s：无 payload 且非 mr，无法重放（provider=%s, event=%s）",
                           row.id, row.provider, row.event_type)
    if re_enqueued:
        logger.info("重试任务 %s：已重新入队（provider=%s）", row.id, row.provider)


def _reviews_runnable(request: Request) -> bool:
    """服务内是否已启动审查 worker（动态判据：worker 懒启动后即为 True）。"""
    return getattr(request.app.state, "worker_pool", None) is not None


async def _ensure_runnable(request: Request) -> None:
    """执行前置守卫：worker 不在则先尝试懒启动（模型/凭据后配的场景），起不来再 409。

    worker 是否存在已不再是启动时定格——保存模型/平台凭据后 `ensure_worker_started`
    会实时解析配置现场拉起；这里兜住「没走到保存钩子」的入口（如 env 配置、其他端点）。
    """
    if _reviews_runnable(request):
        return
    await ensure_worker_started(request.app)
    if not _reviews_runnable(request):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "未配置可用 LLM（或平台适配器），服务未启动审查 worker，"
            "请先在设置页配置模型并保存后再执行",
        )


@router.post("/{task_id}/retry", response_model=TaskRetried)
async def retry_task(
    task_id: int,
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> TaskRetried:
    row = await _get_or_404(session, task_id)
    if not await review_task_allowed(session, user, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    pid = await review_task_project_id(session, row)
    if not await user_can(session, user, "reviews:manage", project_id=pid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限重试该任务")
    if not _retryable(row):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"该任务当前状态不可重试（{row.state}）",
        )
    await _ensure_runnable(request)
    await _requeue_row(request, session, row)
    await session.refresh(row)
    return TaskRetried(id=row.id, state=row.state, attempt=row.attempt)


@router.post("/{task_id}/stop", response_model=TaskStopped)
async def stop_task(
    task_id: int,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> TaskStopped:
    """停止排队中的任务：翻成「未开始」（skipped/manual_stop）。

    不直接操作内存队列（asyncio 队列无按 id 摘除）；残留的队列项被 worker 认领后，
    `ensure_task` 命中未开始行不可抢占 → 自动 no-op 消费掉。running/completed 等其余
    状态不可停止（已在审的任务中断属另一语义，暂不支持）。
    """
    row = await _get_or_404(session, task_id)
    if not await review_task_allowed(session, user, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    pid = await review_task_project_id(session, row)
    if not await user_can(session, user, "reviews:manage", project_id=pid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限停止该任务")
    if row.state != "queued":
        raise HTTPException(status.HTTP_409_CONFLICT, f"仅排队中的任务可停止（{row.state}）")
    row.state = "skipped"
    row.skip_reason = "manual_stop"
    row.error = "排队中被手动停止，未开始审查"
    await session.commit()
    logger.info("任务 %s 已手动停止（queued → 未开始）", row.id)
    return TaskStopped(id=row.id, state=row.state, skip_reason=row.skip_reason)


async def _batch_rows(
    session: AsyncSession, user: CurrentUser, ids: list[int]
) -> list[tuple[int, ReviewTask | None, bool]]:
    """按 ids 取行并做可见范围校验；返回 (请求 id, row 或 None, 是否允许) 保序去重列表。"""
    out: list[tuple[int, ReviewTask | None, bool]] = []
    seen: set[int] = set()
    for tid in ids:
        if tid in seen:
            continue
        seen.add(tid)
        row = (await session.execute(
            select(ReviewTask).where(ReviewTask.id == tid)
        )).scalar_one_or_none()
        if row is None or not await review_task_allowed(session, user, row):
            out.append((tid, None, False))
            continue
        out.append((tid, row, True))
    return out


@router.post("/batch-execute", response_model=BatchResult)
async def batch_execute_tasks(
    body: BatchIdsRequest,
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> BatchResult:
    """批量执行勾选的「未开始」（含 failed 重试）任务：逐行走单条重试逻辑。

    逐行独立判定权限/状态，部分失败不中断其余；结果逐行返回供前端汇总提示。
    """
    results: list[BatchItemResult] = []
    executed = ignored = denied = 0
    # 先尝试懒启动（模型/凭据后配的场景）；起不来不炸批量接口，逐行按 ignored 处理
    if not _reviews_runnable(request):
        try:
            await _ensure_runnable(request)
        except HTTPException:
            pass
    worker_ready = _reviews_runnable(request)
    for tid, row, allowed in await _batch_rows(session, user, body.ids):
        if not allowed or row is None:
            denied += 1
            results.append(BatchItemResult(id=tid, status="denied"))
            continue
        pid = await review_task_project_id(session, row)
        if not await user_can(session, user, "reviews:manage", project_id=pid):
            denied += 1
            results.append(BatchItemResult(id=tid, status="denied"))
            continue
        if not _retryable(row) or not worker_ready:
            ignored += 1
            results.append(BatchItemResult(id=tid, status="ignored"))
            continue
        await _requeue_row(request, session, row)
        executed += 1
        results.append(BatchItemResult(id=tid, status="executed"))
    return BatchResult(executed=executed, ignored=ignored, denied=denied, results=results)


@router.post("/batch-stop", response_model=BatchResult)
async def batch_stop_tasks(
    body: BatchIdsRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> BatchResult:
    """批量停止勾选的「排队中」任务：逐行翻成未开始（manual_stop）。"""
    results: list[BatchItemResult] = []
    executed = ignored = denied = 0
    for tid, row, allowed in await _batch_rows(session, user, body.ids):
        if not allowed or row is None:
            denied += 1
            results.append(BatchItemResult(id=tid, status="denied"))
            continue
        pid = await review_task_project_id(session, row)
        if not await user_can(session, user, "reviews:manage", project_id=pid):
            denied += 1
            results.append(BatchItemResult(id=tid, status="denied"))
            continue
        if row.state != "queued":
            ignored += 1
            results.append(BatchItemResult(id=tid, status="ignored"))
            continue
        row.state = "skipped"
        row.skip_reason = "manual_stop"
        row.error = "排队中被手动停止，未开始审查"
        await session.commit()
        executed += 1
        results.append(BatchItemResult(id=tid, status="executed"))
    return BatchResult(executed=executed, ignored=ignored, denied=denied, results=results)


async def _notify_redeliver_result(
    engine: AsyncEngine, row: ReviewTask, ok: bool, detail: str
) -> None:
    """把重发结果落成系统消息（落库即经 SSE 实时弹给前端，刷新后仍可追认）。

    后台任务没有 HTTP 响应可回，成败原本只进服务日志，用户端零感知；这里落
    system_notification 作为唯一用户感知通道。失败详情带平台原始报错（如 403
    的 URL），extra_data 带任务 id 供前端定位行。落库/广播失败只记日志，不遮蔽
    重发本身的成败。
    """
    try:
        async with session_factory(engine)() as session:
            await SystemNotificationRepository(session).create(
                type="redeliver_done" if ok else "redeliver_failed",
                title=f"任务 {row.id} 评论重发{'成功' if ok else '失败'}",
                message=(
                    "评论已补齐，任务行的「重新发送」按钮会消失。"
                    if ok
                    else f"{detail}\n可检查平台凭据/权限后再次点「重新发送」。"
                ),
                level="success" if ok else "error",
                extra_data={
                    "task_id": row.id,
                    "provider": row.provider,
                    "repo_id": row.repo_id,
                    "pr_number": row.pr_number,
                },
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("重发结果系统消息落库失败（任务 %s）", row.id, exc_info=True)


async def _background_redeliver(
    row: ReviewTask,
    forge: ForgeAdapter,
    repo: ReviewRepository,
    engine: AsyncEngine,
) -> None:
    """后台重发已持久化的审查成果；成功归零 writeback_failed，失败保留标记供再点。

    整个重发**不做**任何 LLM 调用（用落库的 findings + summary_md），只重取 diff +
    回写；`redeliver` 内部自带指纹幂等 + 网络重试，重复点击不会在平台上双发。
    成败各落一条系统消息（SSE 实时弹窗）——后台任务无响应体，这是用户唯一感知通道。
    """
    if row.pr_number is None:
        logger.warning("重发任务 %s：无 PR 号（push 轨），跳过重发", row.id)
        return
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
        await _notify_redeliver_result(engine, row, ok=True, detail="")
    except Exception as exc:  # noqa: BLE001
        # 失败仅记日志并保留 writeback_failed 标记：前端刷新后「重新发送」按钮仍在
        logger.warning("重发失败（任务 %s，provider=%s）：%s", row.id, row.provider, exc)
        await _notify_redeliver_result(
            engine, row, ok=False, detail=f"{type(exc).__name__}: {exc}"
        )


@router.post("/{task_id}/redeliver", response_model=TaskRedelivered)
async def redeliver_task(
    task_id: int,
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> TaskRedelivered:
    """重发已持久化的审查成果（不重算、无 LLM）。仅 `writeback_failed=True` 的任务可重发。

    首次回写失败后成果已在 DB（先落库方案）；这里从 DB 取 findings + summary_md，
    后台 fire-and-forget 走与正常回写同一套 `redeliver`（含指纹幂等）。立即返回
    「已发起」，成功与否在后台完成后翻转 `writeback_failed`，前端刷新可见。
    权限与 retry/stop 同款：先可见范围（越权按 404 不暴露行存在），再 `reviews:manage`。
    """
    row = await _get_or_404(session, task_id)
    if not await review_task_allowed(session, user, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    pid = await review_task_project_id(session, row)
    if not await user_can(session, user, "reviews:manage", project_id=pid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权限重发该任务")
    if not row.writeback_failed:
        raise HTTPException(status.HTTP_409_CONFLICT, "该任务无需重发（writeback_failed 未置位）")
    if row.event_type != "mr" or row.pr_number is None:
        # 重发只针对 mr 轨（评论挂 PR）；push 轨评论挂 commit，走重试而不是重发
        raise HTTPException(status.HTTP_409_CONFLICT, "仅 mr 轨任务可重发评论")
    forge = request.app.state.forge_registry.get(row.provider)
    if forge is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"平台适配器不可用（{row.provider}）")
    # 与 settings.py 同模式：由 app.state.engine 现建仓储（DB 会话按调用自开）
    engine = request.app.state.engine
    repo = ReviewRepository(engine)
    asyncio.create_task(_background_redeliver(row, forge, repo, engine))
    logger.info("已发起重发：任务 %s（provider=%s）", row.id, row.provider)
    return TaskRedelivered(id=row.id, status="redelivering")

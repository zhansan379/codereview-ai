"""定时任务 CRUD REST（主动补拉 / 日报；DESIGN §9 + M5.7）。

`schedule_job` 表由后台「定时任务」页 CRUD 驱动，运行时由 `ScheduleManager` 热更
（保存即生效，无需重启）。`job_type` 限定现存动作：`poll`（补拉轮询，params 含
interval_seconds）| `daily`（日报，params 含 cron 表达式，兼容旧 hour）。

- `GET /schedules`：列出全部任务 + `worker_active`（scheduler 是否在跑）。
- `POST /schedules` / `PUT /schedules/{id}` / `DELETE /schedules/{id}`：CRUD，保存后
  通知 `ScheduleManager.reconcile` 热更。
- `POST /schedules/{id}/run`：立即执行一次该任务动作（scheduler 缺失 → 503）。
读/写无需 worker 也允许（配置先落库，worker 起来后生效）；仅热更/执行在 scheduler 存在时进行。
"""

from __future__ import annotations

from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import get_current_user, get_db, require_permission
from codereview_ai.storage.models import ScheduleJob

router = APIRouter(
    prefix="/schedules",
    dependencies=[Depends(get_current_user), Depends(require_permission("schedules:manage"))],
)

_JOB_TYPES: set[str] = {"poll", "daily"}


class ScheduleJobOut(BaseModel):
    id: int
    name: str
    job_type: str
    enabled: bool
    params: dict[str, Any]


class ScheduleListOut(BaseModel):
    items: list[ScheduleJobOut]
    worker_active: bool


class ScheduleWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    job_type: Literal["poll", "daily"]
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


def _validate(job_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """按 job_type 校验并归一化 params；非法抛 400。

    `daily` **落库统一为 cron 字符串**（兼容旧 `hour`：写入时转 `"0 {hour} * * *"`）；
    cron 经 `CronTrigger.from_crontab` 校验，非法表达式直接 400。
    """
    if job_type == "poll":
        return {"interval_seconds": max(1, int(params.get("interval_seconds", 3600)))}
    if job_type == "daily":
        if "cron" in params:
            cron = str(params["cron"]).strip()
            try:
                CronTrigger.from_crontab(cron)
            except ValueError as exc:
                raise HTTPException(400, f"非法 cron 表达式：{cron}（{exc}）") from exc
            return {"cron": cron}
        hour = int(params.get("hour", 9))
        if not 0 <= hour <= 23:
            raise HTTPException(400, "日报时刻 hour 需在 0-23 之间")
        return {"cron": f"0 {hour} * * *"}
    raise HTTPException(400, f"不支持的定时任务类型：{job_type}")


def _scheduler(request: Request) -> Any:
    return getattr(request.app.state, "scheduler", None)


def _to_out(row: ScheduleJob) -> ScheduleJobOut:
    return ScheduleJobOut(
        id=row.id, name=row.name, job_type=row.job_type,
        enabled=row.enabled, params=row.params or {},
    )


@router.get("", response_model=ScheduleListOut)
async def list_schedules(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ScheduleListOut:
    rows = (await session.execute(select(ScheduleJob).order_by(ScheduleJob.id))).scalars().all()
    return ScheduleListOut(
        items=[_to_out(r) for r in rows],
        worker_active=_scheduler(request) is not None,
    )


@router.post("", response_model=ScheduleJobOut)
async def create_schedule(
    body: ScheduleWrite,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ScheduleJobOut:
    if body.job_type not in _JOB_TYPES:
        raise HTTPException(400, f"不支持的定时任务类型：{body.job_type}")
    existing = (await session.execute(
        select(ScheduleJob).where(ScheduleJob.name == body.name)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"任务名「{body.name}」已存在")
    params = _validate(body.job_type, body.params)
    row = ScheduleJob(
        name=body.name, job_type=body.job_type, enabled=body.enabled, params=params,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    sched = _scheduler(request)
    if sched is not None:
        sched.notify_config_changed()
    return _to_out(row)


@router.put("/{job_id}", response_model=ScheduleJobOut)
async def update_schedule(
    job_id: int,
    body: ScheduleWrite,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ScheduleJobOut:
    row = (await session.execute(
        select(ScheduleJob).where(ScheduleJob.id == job_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "定时任务不存在")
    dup = (await session.execute(
        select(ScheduleJob).where(ScheduleJob.name == body.name, ScheduleJob.id != job_id)
    )).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"任务名「{body.name}」已被其他任务占用")
    params = _validate(body.job_type, body.params)
    row.name = body.name
    row.enabled = body.enabled
    row.params = params
    await session.commit()
    await session.refresh(row)
    sched = _scheduler(request)
    if sched is not None:
        sched.notify_config_changed()
    return _to_out(row)


@router.delete("/{job_id}", response_model=dict)
async def delete_schedule(
    job_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    row = (await session.execute(
        select(ScheduleJob).where(ScheduleJob.id == job_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "定时任务不存在")
    await session.delete(row)
    await session.commit()
    sched = _scheduler(request)
    if sched is not None:
        sched.notify_config_changed()
    return {"id": job_id}


@router.post("/{job_id}/run", response_model=dict)
async def run_schedule(
    job_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    sched = _scheduler(request)
    if sched is None:
        raise HTTPException(503, "运行器未启动，无法立即执行")
    row = (await session.execute(
        select(ScheduleJob).where(ScheduleJob.id == job_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "定时任务不存在")
    import asyncio

    asyncio.create_task(sched.run_job_now(row))
    return {"id": row.id, "name": row.name}

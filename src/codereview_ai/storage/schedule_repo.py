"""定时任务仓库（主动补拉 / 日报；DESIGN §9 + M5.7）。

`schedule_job` 表每行一条待调度任务；本仓库提供 CRUD 读写，**无 TTL 缓存**
（调度循环与后台「定时任务」页都要求即时生效，不走 `ConfigRepository` 的 15 分钟缓存）。
顺序按 id 稳定返回，供 `ScheduleManager.reconcile` 逐任务核算指纹。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ScheduleJob


class ScheduleJobRepository:
    """`schedule_job` 表增删改查；每次调用独立短会话（同 ProjectRepository）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list(self) -> list[ScheduleJob]:
        session = session_factory(self._engine)
        async with session() as s:
            return list((await s.execute(
                select(ScheduleJob).order_by(ScheduleJob.id)
            )).scalars().all())

    async def get(self, job_id: int) -> ScheduleJob | None:
        session = session_factory(self._engine)
        async with session() as s:
            return (await s.execute(
                select(ScheduleJob).where(ScheduleJob.id == job_id)
            )).scalar_one_or_none()

    async def create(self, *, name: str, job_type: str, enabled: bool, params: dict) -> ScheduleJob:
        session = session_factory(self._engine)
        async with session() as s:
            row = ScheduleJob(name=name, job_type=job_type, enabled=enabled, params=params or {})
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    async def update(
        self, job_id: int, *, name: str | None, enabled: bool | None, params: dict | None,
    ) -> ScheduleJob | None:
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ScheduleJob).where(ScheduleJob.id == job_id)
            )).scalar_one_or_none()
            if row is None:
                return None
            if name is not None:
                row.name = name
            if enabled is not None:
                row.enabled = enabled
            if params is not None:
                row.params = params
            await s.commit()
            await s.refresh(row)
            return row

    async def delete(self, job_id: int) -> bool:
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ScheduleJob).where(ScheduleJob.id == job_id)
            )).scalar_one_or_none()
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True

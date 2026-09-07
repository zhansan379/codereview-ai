"""定时任务调度器（主动补拉 / 日报；DESIGN §9 + M5.7）。

`ScheduleManager` 以 `schedule_job` 表为唯一事实源：每条 **enabled** 任务维护一个
独立 asyncio 循环任务，到点执行对应动作（`poll`→`PRPoller.run_once`，`daily`→
`DailyReporter.run_once`）。任何 CRUD/DB 变更通过 `notify_config_changed()` 唤起
`_reconcile_loop`，按 id+job_type+enabled+params 指纹对比——新增则 spawn、停用/删改则
cancel，**配置变更即运行时热更（无需重启）**。单任务异常隔离，不炸整轮。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.ops.periodic import _seconds_until_hour
from codereview_ai.storage.models import ScheduleJob
from codereview_ai.storage.schedule_repo import ScheduleJobRepository

logger = logging.getLogger("codereview_ai.ops.scheduler")

#: 外部 DB 直改（不经 API）时的兜底 reconcile 周期；API 侧变更经 _changed 即刻反映
_RECONCILE_FALLBACK_SECONDS = 60.0


class ScheduleManager:
    """按 `schedule_job` 表驱动定时任务；执行委托给现有 PRPoller / DailyReporter。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        poller: Any | None = None,
        reporter: Any | None = None,
        seed_defaults: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._repo = ScheduleJobRepository(engine)
        self._poller = poller
        self._reporter = reporter
        #: {job_type: {"enabled": bool, "params": {...}}}——空表首次启动时的 env 播种默认
        self._seed_defaults = seed_defaults or {}

        self._stop = asyncio.Event()
        self._changed = asyncio.Event()  # CRUD 后置位，唤起 reconcile
        self._tasks: dict[int, asyncio.Task[None]] = {}  # job_id -> 运行循环
        self._fingerprint: dict[int, str] = {}  # job_id -> 配置指纹（供热更判定）
        self._reconcile_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """空表按 env 播种默认任务，随后启动 reconcile 循环。"""
        await self._seed_if_empty()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        await self.reconcile()

    async def stop(self) -> None:
        """停所有任务并退出 reconcile 循环；幂等。"""
        self._stop.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None
        for jid in list(self._tasks):
            await self._cancel_task(jid)

    async def _seed_if_empty(self) -> None:
        if not self._seed_defaults:
            return
        if await self._repo.list():
            return  # 已有任务 → env 默认不再覆盖
        for job_type, cfg in self._seed_defaults.items():
            params = cfg.get("params") or {}
            enabled = bool(cfg.get("enabled", True))
            name = {"poll": "主动补拉轮询", "daily": "日报"}.get(job_type, job_type)
            await self._repo.create(
                name=name, job_type=job_type, enabled=enabled, params=params,
            )
        logger.info("定时任务空表播种：%s", ", ".join(self._seed_defaults))

    # ------------------------------------------------------------------
    # 热更：reconcile 循环 + 指纹对比
    # ------------------------------------------------------------------

    def notify_config_changed(self) -> None:
        """CRUD 保存后调用：置位事件，唤起 reconcile 立即热更。"""
        self._changed.set()

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=_RECONCILE_FALLBACK_SECONDS)
            except TimeoutError:
                pass  # 兜底：外部直改 DB 也能定期感知
            if self._stop.is_set():
                return
            with contextlib.suppress(asyncio.CancelledError):
                await self.reconcile()

    async def reconcile(self) -> None:
        """按表重算任务集合：新增 spawn、删改 cancel+respawn、停用/删除 cancel。"""
        rows = await self._repo.list()
        desired = {r.id: r for r in rows}
        for jid in list(self._tasks):
            if jid not in desired:
                await self._cancel_task(jid)
        for r in rows:
            fp = self._fingerprint_of(r)
            existing = self._tasks.get(r.id)
            if existing is not None and not existing.done() and self._fingerprint.get(r.id) == fp:
                continue  # 未变更，保持运行
            if existing is not None:
                await self._cancel_task(r.id)
            if r.enabled:
                self._tasks[r.id] = asyncio.create_task(self._run_job(r))
                self._fingerprint[r.id] = fp
            await asyncio.sleep(0)  # 让出事件循环，避免长表阻塞 reconcile

    @staticmethod
    def _fingerprint_of(job: ScheduleJob) -> str:
        return json.dumps(
            [job.job_type, job.enabled, job.params or {}], sort_keys=True, default=str
        )

    async def _cancel_task(self, job_id: int) -> None:
        task = self._tasks.pop(job_id, None)
        self._fingerprint.pop(job_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ------------------------------------------------------------------
    # 单任务循环 + 执行
    # ------------------------------------------------------------------

    async def _run_job(self, job: ScheduleJob) -> None:
        while not self._stop.is_set():
            delay = self._delay_for(job)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # 置位 → 退出
            except asyncio.CancelledError:
                return  # 热更/删除 → 由 reconcile 接管替换
            except TimeoutError:
                pass  # 到点 → 执行一轮
            try:
                await self._run_once(job)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001  单任务异常隔离，不炸循环
                logger.error("定时任务 %s 执行失败：%s", job.name, exc, exc_info=True)

    def _delay_for(self, job: ScheduleJob) -> float:
        """返回距下一次执行的等待秒数（poll→interval；daily→到点小时）。"""
        params = job.params or {}
        if job.job_type == "poll":
            return max(1.0, float(params.get("interval_seconds", 3600)))
        return max(
            0.0, _seconds_until_hour(int(params.get("hour", 9)))
        )

    async def run_job_now(self, job: ScheduleJob) -> None:
        """立即执行一次该任务对应的动作（供「立即执行」按钮）。"""
        await self._run_once(job)

    async def _run_once(self, job: ScheduleJob) -> None:
        if job.job_type == "poll" and self._poller is not None:
            await self._poller.run_once()
        elif job.job_type == "daily" and self._reporter is not None:
            await self._reporter.run_once()
        else:
            logger.warning("定时任务 %s 无可用执行器（job_type=%s）", job.name, job.job_type)

"""定时任务调度器（主动补拉 / 日报；DESIGN §9 + M5.7），基于 APScheduler。

`ScheduleManager` 以 `schedule_job` 表为唯一事实源，把每条 **enabled** 任务映射成
一个 APScheduler job：`poll` → `IntervalTrigger`（间隔秒），`daily` → `CronTrigger`
（落库统一为 cron 字符串，兼容旧 `hour`）。任何 CRUD/DB 变更通过 `notify_config_changed()`
唤起 `_reconcile_loop`，按 id+job_type+enabled+params 指纹对比——新增/改参则
`add_job(replace_existing=True)`、停用/删除则 `remove_job`，**配置变更即热更（无需重启）**。

相较手写 asyncio sleep 循环：获得完整 cron 时间粒度（分/时/日/周几/月）与 interval 单位；
`misfire_grace_time` 放大避免「事件循环稍有延迟即跳过」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncEngine
from tzlocal import get_localzone

from codereview_ai.storage.models import ScheduleJob
from codereview_ai.storage.schedule_repo import ScheduleJobRepository

logger = logging.getLogger("codereview_ai.ops.scheduler")

#: 外部 DB 直改（不经 API）时的兜底 reconcile 周期；API 侧变更经 _changed 即刻反映
_RECONCILE_FALLBACK_SECONDS = 60.0
#: 到点执行出现延迟时的宽限秒数：循环繁忙/事件循环稍有饥饿也不丢触发（不跳跑）
_MISFIRE_GRACE_SECONDS = 3600


class ScheduleManager:
    """按 `schedule_job` 表驱动 APScheduler 任务；执行委托给现有 PRPoller / DailyReporter。"""

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

        self._sched = AsyncIOScheduler(timezone=get_localzone())
        self._stop = asyncio.Event()
        self._changed = asyncio.Event()  # CRUD 后置位，唤起 reconcile
        self._tasks: dict[int, Job] = {}  # job_id -> apscheduler Job（热更判定/测试用）
        self._fingerprint: dict[int, str] = {}  # job_id -> 配置指纹（供热更判定）
        self._reconcile_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """空表按 env 播种默认任务，启动 APScheduler，随后拉起 reconcile。"""
        await self._seed_if_empty()
        self._sched.start()
        await self.reconcile()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        """停 APScheduler 并退出 reconcile 循环；幂等。"""
        self._stop.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None
        self._sched.shutdown(wait=False)
        self._tasks.clear()
        self._fingerprint.clear()

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
    # 热更：reconcile 循环 + 指纹对比 → APScheduler add/remove
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
        """按表重算任务集合：新增/改参 add（replace_existing）、停用/删除 remove。"""
        rows = await self._repo.list()
        desired = {r.id: r for r in rows if r.enabled}
        # 先移除 DB 中已不存在/停用的任务
        for jid in list(self._tasks):
            row = desired.get(jid)
            if row is None:
                await self._remove_job(jid)
            elif self._fingerprint.get(jid) != self._fingerprint_of(row):
                await self._remove_job(jid)  # 指纹变了，下面按新参数重建
        # 新增/重建
        for r in desired.values():
            fp = self._fingerprint_of(r)
            if self._tasks.get(r.id) is not None and self._fingerprint.get(r.id) == fp:
                continue  # 未变更，保持运行
            await self._add_job(r)
            self._fingerprint[r.id] = fp
            await asyncio.sleep(0)  # 让出事件循环，避免长表阻塞 reconcile

    @staticmethod
    def _fingerprint_of(job: ScheduleJob) -> str:
        return json.dumps(
            [job.job_type, job.enabled, job.params or {}], sort_keys=True, default=str
        )

    # ------------------------------------------------------------------
    # APScheduler 任务装配
    # ------------------------------------------------------------------

    def _trigger_for(self, job: ScheduleJob) -> BaseTrigger:
        """按 job_type 构造 APScheduler 触发器。

        - `poll`：interval（`interval_seconds`）。
        - `daily`：优先 `params.cron`（cron 字符串）；兼容旧 `hour`（seed/存量）→ 每日整点；
          缺省 → 每日 9 点。
        """
        params = job.params or {}
        if job.job_type == "poll":
            secs = params.get("interval_seconds", 3600)
            seconds = max(1, int(secs)) if isinstance(secs, (int, str)) else 3600
            return IntervalTrigger(seconds=seconds)
        cron = params.get("cron")
        hour = params.get("hour")
        try:
            if cron:
                return CronTrigger.from_crontab(str(cron))
            if isinstance(hour, (int, str)):
                return CronTrigger(hour=int(hour), minute=0)
            return CronTrigger(hour=9, minute=0)
        except (ValueError, KeyError):
            # 非法 cron/hour（DB 直改绕过 API 校验）→ 回退每日 9 点，不炸 reconcile
            logger.warning("定时任务 %s 时间参数非法，回退每日 9 点：params=%s", job.name, params)
            return CronTrigger(hour=9, minute=0)

    async def _add_job(self, job: ScheduleJob) -> None:
        jid = f"job-{job.id}"
        job_ref = job  # 闭包捕获本轮 DB 行；指纹变化会重建，闭包不持旧参

        async def _run() -> None:
            await self._run_once(job_ref)

        self._sched.add_job(
            _run,
            trigger=self._trigger_for(job),
            id=jid,
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
            max_instances=1,
            coalesce=True,
        )
        existing = self._sched.get_job(jid)
        self._tasks[job.id] = existing

    async def _remove_job(self, job_id: int) -> None:
        self._tasks.pop(job_id, None)
        self._fingerprint.pop(job_id, None)
        with contextlib.suppress(Exception):  # noqa: BLE001  JobLookupError 等
            self._sched.remove_job(f"job-{job_id}")

    async def run_job_now(self, job: ScheduleJob) -> None:
        """立即执行一次该任务对应的动作（供「立即执行」按钮，不排队）。"""
        await self._run_once(job)

    async def _run_once(self, job: ScheduleJob) -> None:
        if job.job_type == "poll" and self._poller is not None:
            await self._poller.run_once()
        elif job.job_type == "daily" and self._reporter is not None:
            await self._reporter.run_once()
        else:
            logger.warning("定时任务 %s 无可用执行器（job_type=%s）", job.name, job.job_type)

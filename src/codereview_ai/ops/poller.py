"""主动补拉 PR/MR（DESIGN §9 补拉通道）：手动按钮 + 定时轮询共用。

webhook 之外补一条**主动通道**：按启用项目调 `ForgeAdapter.list_open_pulls` 列出打开 PR/MR，
给每个未审过的 PR 落一条 `queued` 审计行（入 DB），再入队到 worker 队列，**立即返回**——审查由
worker 异步消费（与 webhook 同一条审查核心 `review_pull_request`，见 worker.py）。补拉本身不再
阻塞在 LLM 耗时上，页面只需要在「入队」这一瞬间等待几秒。

复用跳过靠 `ReviewRepository.poll_skip`：同 head 已有「已处理」的 mr 行（`completed` 已审过 /
`skipped` 门控跳过 / `queued`·`running` 在途）→ 记为 `skipped`、不入队（与审查核心的
`REASON_ALREADY` 语义一致，避免无谓入队）。只有**全新 head** 或 `failed` 头才入队待审——
存量 `mr_disabled` 跳过行因此不再被当成「新入队」重复投队；之后要重审的，走任务行的
「补审」按钮（`force_rerun` 绕过门控）。并发重复审查仍由 worker 内的 per-head 锁兜底。

- `run_once()`：扫全部启用项目一轮，返回报告供接口/日志展示（每项目、每 PR 异常隔离）。
- `run_forever(stop_event, interval)`：asyncio 定时循环，到点跑一轮；`stop_event` 置位即退
  （与 `ops/periodic.DailyReporter.run_forever` 同一模板），随 worker 生命周期清理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.storage.project_repo import ProjectRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import QueueEnqueuer

logger = logging.getLogger("codereview_ai.ops.poller")


class PRPoller:
    """按启用项目主动补拉打开 PR/MR：发现 → 落 queued 行 → 入队异步审查，立即返回。"""

    def __init__(
        self,
        engine: AsyncEngine,
        registry: ForgeRegistry,
        enqueuer: QueueEnqueuer,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._enqueuer = enqueuer
        self._project_repo = ProjectRepository(engine)
        # 手动(`/pulls/poll`)与定时(`run_forever`)共用一把互斥锁：非阻塞抢占，
        # 撞上已在跑的一轮 → 本轮返回 conflict 跳过，杜绝两轮重叠扫同批 PR。
        self._run_lock = asyncio.Lock()
        # 逐条进度：前端 `/pulls/poll/status` 在 running 时读取，展示「已完成 X/Y」。
        # 仅在一轮补拉进行中有效；结束后保留终值但不展示。
        self.progress: dict[str, int] = {"done": 0, "total": 0, "new": 0, "skipped": 0}

    async def run_once(
        self, workspace_ids: set[int] | None = None
    ) -> dict[str, Any]:
        """跑一轮补拉：与在跑的一轮（手动/定时）互斥，撞车则返回 conflict。

        `workspace_ids` 给定（租户自助补拉）→ 只扫该 workspace 集内启用项目（多租户隔离）；
        None → 运营者全量。所有入口共用 `_run_lock`（互斥，撞上一轮在跑 → conflict）。
        手动插口仍保留其 `poll_running` 409 快速拒绝。
        """
        if not self._run_lock.locked():
            async with self._run_lock:
                return await self._run_once_locked(workspace_ids)
        return {"conflict": True, "projects": 0, "prs": 0, "new": 0, "skipped": 0, "errors": []}

    async def _run_once_locked(self, workspace_ids: set[int] | None = None) -> dict[str, Any]:
        """扫（可限定 workspace 集）启用项目一轮：列打开 PR → 逐条跳过已审为【跳过 + 落 queued + 入队】。

        报告字段：`projects`（成功扫描的项目数）、`prs`（打开 PR 总数）、`new`（本次新入队待审）、
        `skipped`（同 head 已审过跳过）、`errors`（每项/每 PR 的失败描述，单向隔离）。
        同时逐条累加 `self.progress`，供前端展示进行中进度（done 含失败项，保证进度前进）。
        """
        report: dict[str, Any] = {"projects": 0, "prs": 0, "new": 0, "skipped": 0, "errors": []}
        self.progress = {"done": 0, "total": 0, "new": 0, "skipped": 0}
        projects = await self._project_repo.list_enabled(workspace_ids)
        if not projects:
            return report
        review_repo = ReviewRepository(self._engine)
        for proj in projects:
            forge = self._registry.get(proj.provider)
            if forge is None:
                continue  # 该平台未配置适配器 → 跳过该项目，不报错
            report["projects"] += 1
            try:
                prs = await forge.list_open_pulls(proj.repo_id)
            except Exception as exc:  # noqa: BLE001  单项目列 PR 失败不中断整轮
                report["errors"].append(f"{proj.provider}@{proj.repo_id} 列打开 PR 失败：{exc}")
                continue
            report["prs"] += len(prs)
            self.progress["total"] = report["prs"]
            for pr in prs:
                if not pr.repo_full_name:
                    pr.repo_full_name = proj.repo_full_name  # 补拉项无路径 → 用项目行
                label = f"{pr.repo_full_name or proj.repo_full_name} pr#{pr.pr_number}"
                try:
                    # 1) 同 head 已有「已处理」的 mr 行（completed 已审 / skipped 门控跳过 /
                    #    queued·running 在途）→ 跳过，不入队（仅新 head 或 failed 头入队待审）。
                    if await review_repo.poll_skip(
                        proj.provider, proj.repo_id, pr.pr_number, pr.head_sha
                    ):
                        report["skipped"] += 1
                        self.progress["skipped"] += 1
                        continue
                    # 2) 落 queued 审计行（幂等键抢占，在审/已排队则入队后 core 仍会去重）。
                    await review_repo.ensure_task(
                        provider=proj.provider,
                        repo_id=proj.repo_id,
                        pr_number=pr.pr_number,
                        event_type="mr",
                        branch=pr.source_branch,
                        head_sha=pr.head_sha,
                        base_sha=pr.base_sha,
                        pr_title=pr.title,
                        web_url=pr.web_url,
                        payload="",  # 已解析 PR 直接入队，无需原始 body
                    )
                    # 3) 入队异步审查，不等待 LLM 完成。
                    await self._enqueuer.enqueue_pr(proj.provider, pr)
                    report["new"] += 1  # 已入队，待 worker 异步审查
                    self.progress["new"] += 1
                except Exception as exc:  # noqa: BLE001  单 PR 失败隔离，不中断整轮
                    report["errors"].append(f"{label}：{exc}")
                finally:
                    self.progress["done"] += 1
        return report

    async def run_forever(self, stop_event: asyncio.Event, interval: float) -> None:
        """定时轮询循环：每 `interval` 秒跑一轮，直到 `stop_event` 置位（随 worker 清理）。"""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # 置位 → 退出
            except TimeoutError:
                logger.info("补拉定时到点，启动一轮主动拉取")
                try:
                    report = await self.run_once()
                    if report.get("conflict"):
                        logger.info("补拉到点：上一轮仍进行中，本轮跳过")
                    elif report["errors"]:
                        logger.warning("补拉完成：新入队 %s/跳过 %s/错误 %s（%s）",
                                       report["new"], report["skipped"], len(report["errors"]),
                                       report["errors"][0])
                    else:
                        logger.info("补拉完成：新入队 %s/跳过 %s/共查 %s 个打开 PR",
                                    report["new"], report["skipped"], report["prs"])
                except Exception as exc:  # noqa: BLE001  定时轮询失败只记日志，不炸进程
                    logger.error("补拉异常：%s", exc, exc_info=True)
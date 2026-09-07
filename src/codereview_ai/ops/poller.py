"""主动补拉 PR/MR（DESIGN §9 补拉通道）：手动按钮 + 定时轮询共用。

webhook 之外补一条**主动通道**：按启用项目调 `ForgeAdapter.list_open_pulls` 列出打开 PR/MR，
逐个进 `review_pull_request`（与 webhook 同一条审查核心，见 worker.py）。幂等靠 `ensurure_task`
按 head_sha 的唯一索引兜底——已审过的同 head 由 `review_pull_request` 内置的增量决策返回
`"already"`，天然跳过，不重复刷 token/评论。

- `run_once()`：扫全部启用项目一轮，返回报告供接口/日志展示（每项目、每 PR 异常隔离）。
- `run_forever(stop_event, interval)`：asyncio 定时循环，到点跑一轮；`stop_event` 置位即退
  （与 `ops/periodic.DailyReporter.run_forever` 同一模板），随 worker 生命周期清理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.forges.registry import ForgeRegistry
from codereview_ai.notifiers.dispatch import NotifierDispatcher
from codereview_ai.review.agentic.llmloop import AgentLLM
from codereview_ai.review.agentic.sandbox import SandboxRuntime
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.review.static_analysis import StaticAnalyzer
from codereview_ai.storage.project_repo import ProjectRepository
from codereview_ai.storage.review_repo import ReviewRepository
from codereview_ai.worker import ProjectConfigFactory, review_pull_request

logger = logging.getLogger("codereview_ai.ops.poller")


class PRPoller:
    """按启用项目主动补拉打开 PR/MR，复用 worker 审查核心（幂等去重）。"""

    def __init__(
        self,
        engine: AsyncEngine,
        registry: ForgeRegistry,
        reviewer: Reviewer,
        *,
        notifier: NotifierDispatcher | None = None,
        grouper: SemanticGrouper | None = None,
        chain_valid: Callable[[str, str], bool] | None = None,
        static_analyzer: StaticAnalyzer | None = None,
        review_strategy: str = "diff",
        agent_runtime: SandboxRuntime | None = None,
        agent_llm_factory: Callable[[], AgentLLM] | None = None,
        project_config_factory: ProjectConfigFactory | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._reviewer = reviewer
        self._project_repo = ProjectRepository(engine)
        self._notifier = notifier
        self._grouper = grouper
        self._chain_valid = chain_valid
        self._static_analyzer = static_analyzer
        self._review_strategy = review_strategy
        self._agent_runtime = agent_runtime
        self._agent_llm_factory = agent_llm_factory
        self._project_config_factory = project_config_factory

    async def run_once(self) -> dict[str, Any]:
        """扫全部启用项目一轮：列打开 PR → 逐个审查（幂等去重），返回汇总报告。

        报告字段：`projects`（成功扫描的项目数）、`prs`（打开 PR 总数）、`new`（本次新审）、
        `skipped`（同 head 已审过跳过）、`errors`（每项/每 PR 的失败描述，单向隔离）。
        """
        report: dict[str, Any] = {"projects": 0, "prs": 0, "new": 0, "skipped": 0, "errors": []}
        projects = await self._project_repo.list_enabled()
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
            for pr in prs:
                if not pr.repo_full_name:
                    pr.repo_full_name = proj.repo_full_name  # 补拉项无路径 → 用项目行
                try:
                    status = await review_pull_request(
                        forge, self._reviewer, pr,
                        review_repo=review_repo, grouper=self._grouper,
                        chain_valid=self._chain_valid, notifier=self._notifier,
                        static_analyzer=self._static_analyzer,
                        review_strategy=self._review_strategy,
                        agent_runtime=self._agent_runtime,
                        agent_llm_factory=self._agent_llm_factory,
                        project_config_factory=self._project_config_factory,
                    )
                except Exception as exc:  # noqa: BLE001  单 PR 失败隔离，不中断整轮
                    report["errors"].append(
                        f"{pr.repo_full_name or proj.repo_full_name} pr#{pr.pr_number}：{exc}"
                    )
                    continue
                if status == "already":
                    report["skipped"] += 1
                else:
                    report["new"] += 1  # reviewed / empty 都算本次已处理
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
                    if report["errors"]:
                        logger.warning("补拉完成：新 %s/跳过 %s/错误 %s（%s）",
                                       report["new"], report["skipped"], len(report["errors"]),
                                       report["errors"][0])
                    else:
                        logger.info("补拉完成：新 %s/跳过 %s/共查 %s 个打开 PR",
                                    report["new"], report["skipped"], report["prs"])
                except Exception as exc:  # noqa: BLE001  定时轮询失败只记日志，不炸进程
                    logger.error("补拉异常：%s", exc, exc_info=True)
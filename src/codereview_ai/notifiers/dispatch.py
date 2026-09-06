"""通知分发（DESIGN F4）：按路由（渠道 + 项目级覆盖）推送，带 @ 阈值门控 + 指数退避重试。

- `NotifierDispatcher`：从 `ConfigRepository.notifier_routes()` 拉路由 → 逐条构建 sink →
  按 F4.3 @阈值决定是否 @提交者 → 发送（指数退避重试）。
- **失败不炸主流程**：单渠道失败仅记日志继续；`launch` 把它丢进后台任务（fire-and-forget），
  审查主链不被推送拖慢或中断。
- 未实现的渠道记日志跳过，不至于因配置了未知 channel 而整体失败。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable

import httpx

from codereview_ai.config.repository import NotifierRoute
from codereview_ai.domain.models import PullRequest, ReviewResult
from codereview_ai.notifiers.base import Notifier, ReviewNotification, build_review_notification
from codereview_ai.notifiers.dingtalk import DingTalkNotifier
from codereview_ai.notifiers.feishu import FeishuNotifier
from codereview_ai.notifiers.wecom import WeComNotifier

logger = logging.getLogger("codereview_ai.notifiers.dispatch")

#: 已实现的渠道 → sink 类（新增渠道在此登记，dispatch 无需改动）
_SINKS: dict[str, Callable[..., Notifier]] = {
    "dingtalk": DingTalkNotifier,
    "feishu": FeishuNotifier,
    "wecom": WeComNotifier,
}

RoutesProvider = Callable[[int | None], Awaitable[list[NotifierRoute]]]


def build_notifier(route: NotifierRoute, *, http: httpx.AsyncClient) -> Notifier | None:
    """按路由构建渠道 sink；未实现的渠道返回 None（上层记日志跳过）。"""
    cls = _SINKS.get(route.channel)
    if cls is None:
        logger.warning("未实现的推送渠道 %s，跳过", route.channel)
        return None
    return cls(route.webhook, route.secret or "", http=http)


class NotifierDispatcher:
    """把一条审查通知按所有启用路由推送出去；失败不抛出，交由 launch 后台化。"""

    def __init__(
        self,
        routes: RoutesProvider,
        *,
        http: httpx.AsyncClient | None = None,
        attempts: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._routes = routes
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._attempts = max(1, attempts)
        self._backoff_base = backoff_base

    def _apply_at_threshold(
        self, msg: ReviewNotification, route: NotifierRoute
    ) -> ReviewNotification:
        """F4.3：评分低于 at_threshold 才 @提交者，否则清空 at 列表。"""
        at = msg.at_users
        if not (route.at_threshold and msg.score is not None and msg.score < route.at_threshold):
            at = []
        return dataclasses.replace(msg, at_users=at)

    async def _send_with_retry(self, sink: Notifier, msg: ReviewNotification) -> None:
        """指数退避重试：1s/2s/4s…（DESIGN F4.4，asyncio.sleep，不阻塞事件循环）。"""
        last: Exception | None = None
        for attempt in range(self._attempts):
            try:
                await sink.send(msg)
                return
            except Exception as exc:  # noqa: BLE001  网络/5xx，退避重试
                last = exc
                if attempt < self._attempts - 1:
                    await asyncio.sleep(self._backoff_base * (2**attempt))
        raise RuntimeError(f"渠道 {sink.channel} 重试 {self._attempts} 次仍失败") from last

    async def dispatch(
        self, pr: PullRequest, result: ReviewResult, *, project_id: int | None = None
    ) -> int:
        """推送一次审查结果到所有启用路由，返回成功渠道数；失败不抛出。"""
        routes = await self._routes(project_id)
        base = build_review_notification(pr, result, at_users=[pr.author] if pr.author else [])
        sent = 0
        for route in routes:
            sink = build_notifier(route, http=self._http)
            if sink is None:
                continue
            try:
                await self._send_with_retry(sink, self._apply_at_threshold(base, route))
                sent += 1
            except Exception as exc:  # noqa: BLE001  单渠道失败不炸主流程
                logger.error("渠道 %s 推送失败: %s", route.channel, exc, exc_info=True)
        return sent

    def launch(
        self, pr: PullRequest, result: ReviewResult, *, project_id: int | None = None
    ) -> asyncio.Task[None]:
        """fire-and-forget：后台任务推送，异常仅记录，不阻碍/不拖慢审查主链（F4.4）。"""
        return asyncio.create_task(self._guarded(pr, result, project_id))

    async def send_markdown(
        self, title: str, markdown: str, *, project_id: int | None = None,
    ) -> int:
        """推一条**纯文本/markdown**到匹配路由（日报等无 PR 上下文的推送）。

        复用同一套渠道 sink 与指数退避重试；失败只记日志不抛出（M5.7 日报不炸进程）。
        `score=None` → 不触发 @阈值门控（@ 仅适用于审查结果）。
        """
        msg = ReviewNotification(
            project_name="代码审查日报", title=title, score=None,
            summary_md=markdown, url="",
        )
        routes = await self._routes(project_id)
        sent = 0
        for route in routes:
            sink = build_notifier(route, http=self._http)
            if sink is None:
                continue
            try:
                await self._send_with_retry(sink, msg)
                sent += 1
            except Exception as exc:  # noqa: BLE001  单渠道失败不炸流程
                logger.error("渠道 %s 推送日报失败: %s", route.channel, exc, exc_info=True)
        return sent

    async def _guarded(
        self, pr: PullRequest, result: ReviewResult, project_id: int | None
    ) -> None:
        try:
            await self.dispatch(pr, result, project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("推送任务异常: %s", exc, exc_info=True)

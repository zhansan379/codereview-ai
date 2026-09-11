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
from codereview_ai.storage.models import NotifierMember

logger = logging.getLogger("codereview_ai.notifiers.dispatch")

#: 已实现的渠道 → sink 类（新增渠道在此登记，dispatch 无需改动）
_SINKS: dict[str, Callable[..., Notifier]] = {
    "dingtalk": DingTalkNotifier,
    "feishu": FeishuNotifier,
    "wecom": WeComNotifier,
}

RoutesProvider = Callable[[int | None], Awaitable[list[NotifierRoute]]]
MemberResolver = Callable[[str], Awaitable[NotifierMember | None]]


def member_to_platform_id(member: NotifierMember, channel: str) -> str | None:
    """把系统级成员映射成某渠道的认识的 @ID；该平台没有标识返回 None（跳过不 @）。

    dingtalk→手机号 / feishu→open_id / wecom→userid。fork username 本身任何平台
    都不认识，绝不落回来——它只走 `mention_names`（文案点名）。
    """
    match channel:
        case "dingtalk":
            return member.dingtalk_mobile or None
        case "feishu":
            return member.feishu_open_id or None
        case "wecom":
            return member.wecom_userid or None
    return None


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
        resolve_member: MemberResolver | None = None,
        attempts: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._routes = routes
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._resolve_member = resolve_member  # 作者 git_username → 系统级成员
        self._attempts = max(1, attempts)
        self._backoff_base = backoff_base

    async def _compose_at(
        self, pr: PullRequest, route: NotifierRoute
    ) -> list[str]:
        """按渠道解析一条路由的 @ID：静态勾选成员 + 动态作者（git_username 命中）。

        两者都经 `member_to_platform_id` 转成平台认识的 ID；解析不到（作者非成员表
        用户、或该平台无标识）就自然丢出列表——fork username 永不进 at。
        """
        at: list[str] = []
        for m in route.at_members:
            if (mid := member_to_platform_id(m, route.channel)) and mid not in at:
                at.append(mid)
        if pr.author and self._resolve_member is not None:
            author = await self._resolve_member(pr.author)
            if author and (aid := member_to_platform_id(author, route.channel)) and aid not in at:
                at.append(aid)
        return at

    def _apply_at_threshold(
        self, msg: ReviewNotification, route: NotifierRoute
    ) -> ReviewNotification:
        """F4.3 门控：评分低于 at_threshold 才保留 @（静态成员 + 作者 + @全员），否则清空。"""
        at = msg.at_users
        at_all = msg.at_all
        if not (route.at_threshold and msg.score is not None and msg.score < route.at_threshold):
            at = []
            at_all = False
        return dataclasses.replace(msg, at_users=at, at_all=at_all)

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
        base = build_review_notification(pr, result)
        sent = 0
        for route in routes:
            sink = build_notifier(route, http=self._http)
            if sink is None:
                continue
            # @ID 每条路由单独组装（静态成员 + 作者解析 + @全员开关），再统一过 @ 阈值门控
            msg = dataclasses.replace(
                base,
                at_users=await self._compose_at(pr, route),
                at_all=route.at_all,
            )
            msg = self._apply_at_threshold(msg, route)
            try:
                await self._send_with_retry(sink, msg)
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
            # 日报/汇总类：企微走 markdown_v2（表格可渲染；无 @ 需求，v2 能力对齐）
            render_v2=True,
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

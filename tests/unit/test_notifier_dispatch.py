"""通知分发测试（DESIGN F4.2-F4.4）：@阈值门控、指数退避重试、失败不炸主流程。

离线：httpx.MockTransport 记录请求，注入 fake routes；断言按 project_id 路由过滤、
评分低于 at_threshold 才 @提交者、5xx 退避重试后成功、未知渠道跳过、全失败不抛出。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from codereview_ai.config.repository import NotifierRoute
from codereview_ai.domain.models import (
    Category,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
    Severity,
)
from codereview_ai.notifiers.dispatch import NotifierDispatcher


def _pr(author: str = "alice") -> PullRequest:
    return PullRequest(provider="gitlab", repo_id="7", repo_full_name="a/b",
                       web_url="https://x/mr", pr_number=3, title="修复登录",
                       source_branch="f", target_branch="m", head_sha="h", base_sha="b",
                       author=author)


def _result(score: int) -> ReviewResult:
    r = ReviewResult(summary="汇总")
    r.scores = ReviewScores(correctness=score)
    r.findings = [Finding(content="x", category=Category.BUG, severity=Severity.HIGH,
                          existing_code="e", file="a.py")]
    return r


def _req_json(req: httpx.Request) -> dict:
    return json.loads(req.content)


def _route(channel: str = "dingtalk", at_threshold: int = 60) -> NotifierRoute:
    return NotifierRoute(channel=channel, webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
                         secret="s", project_id=None, at_threshold=at_threshold)


async def _capture_collector(captured: list, *, status=200, fail_first=0):
    """返回一个有状态的 transport handler：前 fail_first 次返回 5xx，之后按 status。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        calls["n"] += 1
        if calls["n"] <= fail_first:
            return httpx.Response(502, json={"errcode": 1})
        return httpx.Response(status, json={"errcode": 0})

    return handler


async def test_dispatch_filters_routes_by_project_and_sends():
    """F4.2：路由按 project_id 过滤（NULL 全局默认始终适用）。"""
    captured: list[httpx.Request] = []
    global_route = _route()
    project_route = NotifierRoute(channel="dingtalk", webhook="https://w/x",
                                  secret="", project_id=7, at_threshold=50)

    async def routes(project_id: int | None):
        return [r for r in (global_route, project_route) if r.project_id in (None, project_id)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        sent = await disp.dispatch(_pr(), _result(85), project_id=None)
    assert sent == 1  # 只命中全局默认，项目级 7 不匹配 None


async def test_at_threshold_carries_mention_when_score_low():
    """F4.3：评分 < at_threshold → 带入 at_all/at_targets 渲染 @；≥ → 全清空。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [NotifierRoute(channel="dingtalk", webhook="https://oapi.dingtalk.com/robot/send?"
                                                           "access_token=x", secret="s",
                              project_id=None, at_threshold=60,
                              at_all=True, at_targets=[{"author": "alice", "mobile": "13800000000"}])]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        await disp.dispatch(_pr(), _result(50))  # 低于 60
        at = _req_json(captured[0])["at"]
        assert at["atMobiles"] == ["13800000000"] and at["isAtAll"] is True
        captured.clear()
        await disp.dispatch(_pr(), _result(85))  # 高于 60 → 清空
        assert _req_json(captured[0])["at"] == {"atMobiles": [], "isAtAll": False}


async def test_retry_with_backoff_on_5xx():
    """F4.4：前 2 次 502 → 第 3 次成功，重试期间指数退避（1s/2s），最终 sent=1。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [_route()]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured, fail_first=2))
    ) as client:
        disp = NotifierDispatcher(routes, http=client, attempts=3, backoff_base=0.005)
        sent = await disp.dispatch(_pr(), _result(85))
    assert sent == 1
    assert len(captured) == 3  # 重试 3 次（2 败 + 1 成）


async def test_all_fail_does_not_raise_and_counts_zero():
    """渠道始终 5xx：dispatch 不抛出，返回 0，不影响后续渠道。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [_route(at_threshold=200)]  # 高分仍重试失败渠道

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured, status=500))
    ) as client:
        disp = NotifierDispatcher(routes, http=client, attempts=2, backoff_base=0.005)
        sent = await disp.dispatch(_pr(), _result(85))
    assert sent == 0


async def test_unknown_channel_skipped():
    """未实现渠道 build_notifier 返回 None → dispatcher 跳过，不计数不报错。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [NotifierRoute(channel="slack", webhook="https://w/x", secret="",
                              project_id=None, at_threshold=60)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        sent = await disp.dispatch(_pr(), _result(85))
    assert sent == 0
    assert captured == []


async def test_launch_is_fire_and_forget_but_awaited_task_succeeds():
    """launch 后台推送：任务完成且异常不向上抛（F4.4 fire-and-forget）。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [_route()]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        task = disp.launch(_pr(), _result(85))
        await asyncio.wait_for(task, timeout=5)
    assert len(captured) == 1

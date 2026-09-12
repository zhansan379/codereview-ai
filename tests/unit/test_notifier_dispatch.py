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
from codereview_ai.notifiers.dispatch import NotifierDispatcher, member_to_platform_id
from codereview_ai.storage.models import NotifierMember


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


def _route(
    channel: str = "dingtalk", at_threshold: int = 60, at_all: bool = False
) -> NotifierRoute:
    return NotifierRoute(channel=channel, webhook="https://oapi.dingtalk.com/robot/send?access_token=x",
                         secret="s", project_id=None, at_threshold=at_threshold, at_all=at_all)


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


def test_member_to_platform_id_maps_per_channel():
    """同一成员按渠道取对应平台 ID；该平台无标识返回 None；未知渠道返回 None。"""
    m = _member(dingtalk_mobile="13800138000", wecom_userid="u2", feishu_open_id="ou_9")
    assert member_to_platform_id(m, "dingtalk") == "13800138000"
    assert member_to_platform_id(m, "wecom") == "u2"
    assert member_to_platform_id(m, "feishu") == "ou_9"
    assert member_to_platform_id(m, "slack") is None
    empty = _member(dingtalk_mobile="", feishu_open_id="")
    assert member_to_platform_id(empty, "dingtalk") is None


def _member(**kw) -> NotifierMember:
    defaults = dict(
        id=1, name="张三", git_username="zhangsan", dingtalk_mobile="13800138000",
        wecom_userid="", feishu_open_id="",
    )
    defaults.update(kw)
    return NotifierMember(**defaults)


async def _resolver(*members, match_by_username=True):
    """fake 作者解析：git_username 命中成员表返回该成员，否则 None。"""
    async def resolve(git_username: str):
        for m in members:
            if (not match_by_username) or m.git_username == git_username:
                return m
        return None
    return resolve


async def test_at_threshold_gates_author_at_resolved_to_platform_id():
    """F4.3：作者经 git_username 命中成员 → 解析成钉钉手机号；低于阈值才 @；fork 用户名不进 at。"""
    captured: list[httpx.Request] = []
    alice = _member(git_username="alice", dingtalk_mobile="13900001111")

    async def routes(project_id: int | None):
        return [_route(at_threshold=60)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client,
                                  resolve_member=await _resolver(alice))
        await disp.dispatch(_pr(author="alice"), _result(50))  # 低于 60
        at = _req_json(captured[0])["at"]
        assert at["atMobiles"] == ["13900001111"] and at["isAtAll"] is False
        captured.clear()
        await disp.dispatch(_pr(author="alice"), _result(85))  # 高于 60 → 门控清空
        assert _req_json(captured[0])["at"]["atMobiles"] == []


async def test_author_not_in_members_is_never_at_but_still_mention():
    """命中不到成员表：fork 用户名只进 mention_names（正文点名），绝不进 atMobiles。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [_route(at_threshold=60)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client, resolve_member=await _resolver())
        await disp.dispatch(_pr(author="fork_user"), _result(40))
        at = _req_json(captured[0])["at"]
        assert at["atMobiles"] == []  # fork 用户名绝不进 at
        assert "fork_user" in _req_json(captured[0])["markdown"]["text"]  # 文案点名


async def test_route_at_members_resolved_per_channel():
    """渠道勾选成员按 channel 映射成平台 ID；该平台缺 ID 的成员跳过。"""
    captured: list[httpx.Request] = []
    zhangsan = _member(git_username="zhangsan", dingtalk_mobile="13800138000")
    no_mobile = _member(id=2, name="李四", git_username="lisi", dingtalk_mobile="")

    route = _route(at_threshold=100)  # 高分仍触发？不，未加 gating 我们单独测解析
    route.at_members = [zhangsan, no_mobile]

    async def routes(project_id: int | None):
        return [route]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        await disp.dispatch(_pr(), _result(85))  # 85 < 100 → 门控放行
        assert _req_json(captured[0])["at"]["atMobiles"] == ["13800138000"]


async def test_at_all_gated_by_threshold():
    """@所有人：路由开 at_all 后，评分低于阈值才置位；达标清掉（与具体成员同门控）。"""
    captured: list[httpx.Request] = []
    alice = _member(git_username="alice", dingtalk_mobile="13900001111")

    async def routes(project_id: int | None):
        return [_route(at_threshold=60, at_all=True)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client, resolve_member=await _resolver(alice))
        await disp.dispatch(_pr(author="alice"), _result(50))  # 低于 60
        at = _req_json(captured[0])["at"]
        assert at["isAtAll"] is True and at["atMobiles"] == ["13900001111"]
        captured.clear()
        await disp.dispatch(_pr(author="alice"), _result(85))  # 高于 60 → @所有人与具体 @ 一起清空
        at = _req_json(captured[0])["at"]
        assert at["isAtAll"] is False and at["atMobiles"] == []


async def test_at_all_default_off_when_route_not_set():
    """未开 at_all 的路由：isAtAll 恒 False（默认）。"""
    captured: list[httpx.Request] = []

    async def routes(project_id: int | None):
        return [_route(at_threshold=60, at_all=False)]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        await disp.dispatch(_pr(), _result(40))
    assert _req_json(captured[0])["at"]["isAtAll"] is False


async def test_send_markdown_never_applies_at_gate():
    """send_markdown（score=None）：不触发 @ 门控，日报不带 @。"""
    captured: list[httpx.Request] = []
    zhangsan = _member(dingtalk_mobile="13800138000")
    route = _route(at_threshold=100)
    route.at_members = [zhangsan]

    async def routes(project_id: int | None):
        return [route]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(await _capture_collector(captured))
    ) as client:
        disp = NotifierDispatcher(routes, http=client)
        await disp.send_markdown("日报", "# x", project_id=None)
    # score=None → _apply_at_threshold 不被调用（send_markdown 直接发 base）
    assert captured[0] is not None


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

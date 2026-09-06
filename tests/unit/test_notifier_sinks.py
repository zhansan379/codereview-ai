"""IM 各渠道 sink 测试（DESIGN F4）：签名 / 消息体 / 超长截断 / 非 2xx 抛异常。

离线：httpx.MockTransport 捕获请求，断言钉钉加签 URL 每次含新 timestamp、payload 结构、
钉钉 markdown 超长截断到 20000 字节、企业微信 content 截断到 4096 且不切断多字节字符。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from codereview_ai.domain.models import (
    Category,
    Finding,
    PullRequest,
    ReviewResult,
    ReviewScores,
    Severity,
)
from codereview_ai.notifiers.base import build_review_notification, truncate_utf8
from codereview_ai.notifiers.dingtalk import DingTalkNotifier
from codereview_ai.notifiers.wecom import WeComNotifier

DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=abc"

REPLACEMENT = "�"  # U+FFFD：多字节字符被切断时的乱码码元


def _pr() -> PullRequest:
    return PullRequest(provider="gitlab", repo_id="7", repo_full_name="a/b", web_url="https://x/mr",
                       pr_number=3, title="修复登录", source_branch="f", target_branch="m",
                       head_sha="h", base_sha="b", author="alice")


def _result(score: int, findings: list[Finding]) -> ReviewResult:
    r = ReviewResult(summary="发现若干问题，建议合入前修复。")
    r.scores = ReviewScores(correctness=score)
    r.findings = findings
    return r


def _finding(sev: Severity) -> Finding:
    return Finding(content="问题", category=Category.BUG, severity=sev,
                   existing_code="x", file="a.py")


def _make_client(captured: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"errcode": 0})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _req_json(req: httpx.Request) -> dict:
    return json.loads(req.content)


async def test_dingtalk_sign_per_send_and_payload():
    """DESIGN F4 / reference §1：加签每次 send 都算，payload 结构正确、at 透传。"""
    captured: list[httpx.Request] = []
    client = _make_client(captured)
    notifier = DingTalkNotifier(DINGTALK_WEBHOOK, secret="s3cret", http=client)
    msg = build_review_notification(_pr(), _result(85, [_finding(Severity.HIGH)]),
                                    at_users=["alice"])
    await notifier.send(msg)

    req = captured[0]
    # 加签：URL 带 timestamp 与 sign
    assert "timestamp=" in str(req.url) and "sign=" in str(req.url)
    body = _req_json(req)
    assert body["msgtype"] == "markdown"
    assert body["markdown"]["title"] == "代码审查报告"
    assert "总分：**85**" in body["markdown"]["text"]
    assert body["at"] == {"atMobiles": ["alice"], "isAtAll": False}
    await client.aclose()


async def test_dingtalk_no_secret_skip_sign():
    """secret 为空时不加签：URL 不带 sign（DESIGN：部分渠道不签名）。"""
    captured: list[httpx.Request] = []
    client = _make_client(captured)
    notifier = DingTalkNotifier(DINGTALK_WEBHOOK, secret="", http=client)
    await notifier.send(build_review_notification(_pr(), _result(85, [])))
    assert "sign=" not in str(captured[0].url)
    await client.aclose()


async def test_dingtalk_sign_differs_between_calls():
    """问题 1 回归：进程常驻下两次 send 的 timestamp 不能相同（否则 1 小时后失效）。"""
    captured: list[httpx.Request] = []
    client = _make_client(captured)
    notifier = DingTalkNotifier(DINGTALK_WEBHOOK, secret="s", http=client)
    msg = build_review_notification(_pr(), _result(85, []))
    await notifier.send(msg)
    await asyncio.sleep(0.005)  # 保证两次 send 的毫秒级 timestamp 不同
    await notifier.send(msg)
    assert len(captured) == 2
    url1, url2 = str(captured[0].url), str(captured[1].url)
    assert url1 != url2
    await client.aclose()


async def test_dingtalk_long_summary_truncated():
    """钉钉 markdown 超 20000 字节必须截断到上限内、且不切断多字节字符。"""
    captured: list[httpx.Request] = []
    client = _make_client(captured)
    notifier = DingTalkNotifier(DINGTALK_WEBHOOK, http=client)
    msg = build_review_notification(_pr(), _result(85, []))
    msg.summary_md = "好" * 30000  # 每字 3 字节 → 远超 20000
    await notifier.send(msg)
    text = _req_json(captured[0])["markdown"]["text"]
    assert len(text.encode("utf-8")) <= notifier.max_text_bytes + 200  # 标题等内容余量
    assert REPLACEMENT not in text
    await client.aclose()


async def test_sink_raises_on_non_2xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errcode": 1, "errmsg": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = DingTalkNotifier(DINGTALK_WEBHOOK, http=client)
        msg = build_review_notification(_pr(), _result(85, []))
        with pytest.raises(RuntimeError):
            await notifier.send(msg)


async def test_wecom_content_truncated_to_4096_bytes():
    """企业微信 content 上限 4096 字节（reference §3 限制），超长截断到上限内。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"errcode": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = WeComNotifier(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", http=client
        )
        msg = build_review_notification(_pr(), _result(85, []))
        msg.summary_md = "长" * 10000
        await notifier.send(msg)
    assert captured
    content = _req_json(captured[0])["markdown"]["content"]
    assert len(content.encode("utf-8")) <= 4096


def test_truncate_utf8_keeps_char_boundary():
    s = ("正常" * 500) + ("🚀" * 500)
    out = truncate_utf8(s, 60)
    assert len(out.encode("utf-8")) <= 60
    assert REPLACEMENT not in out
    assert out.endswith("…")

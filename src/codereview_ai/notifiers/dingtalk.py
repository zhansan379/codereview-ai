"""钉钉自定义机器人（加签）markdown 推送 sink（reference/im_payloads.md §1）。

对照原实现必须改的问题（§5）：
- **加签在每次 send 时计算**（问题 1）——进程常驻超 1 小时不失效。
- 超长按 20000 字节截断（问题：原来只能整体发，超了就失败）。
- 只往 URL 拼 signature，不把含 token 的完整 URL 打进日志。
- timeout 由注入的 httpx client 统一约束（问题 4：零 timeout）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from codereview_ai.notifiers.base import ReviewNotification, truncate_utf8

logger = logging.getLogger("codereview_ai.notifiers.dingtalk")

_HEADER = "#### 代码审查报告"


class DingTalkNotifier:
    """钉钉自定义机器人 markdown 推送；构造时注入 http 客户端（测试用 MockTransport）。"""

    channel = "dingtalk"
    max_text_bytes = 20000

    def __init__(self, webhook: str, secret: str = "", *, http: httpx.AsyncClient) -> None:
        self._webhook = webhook
        self._secret = secret
        self._http = http

    def sign_url(self, webhook: str, secret: str) -> str:
        """钉钉「加签」URL 构造：每次发送重新计算（DESIGN：timestamp 与服务端时差≤1h）。"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{webhook}&timestamp={timestamp}&sign={sign}"

    def _render_text(self, msg: ReviewNotification) -> str:
        lines = [_HEADER, f"#### {msg.project_name} {msg.title}", ""]
        if msg.score is not None:
            lines.append(f"- 总分：**{msg.score}**")
        if msg.findings_count:
            parts = "、".join(f"{sev} ×{n}" for sev, n in sorted(msg.findings_count.items()))
            lines.append(f"- 发现：{parts}")
        if msg.mention_names:
            lines.append("- 相关：@" + "、@".join(msg.mention_names))
        lines.append("")
        lines.append(truncate_utf8(msg.summary_md, self.max_text_bytes))
        lines.append(f"\n[查看详情]({msg.url})")
        return "\n".join(lines)

    async def send(self, msg: ReviewNotification) -> None:
        url = self.sign_url(self._webhook, self._secret) if self._secret else self._webhook
        payload: dict[str, object] = {
            "msgtype": "markdown",
            "markdown": {"title": "代码审查报告", "text": self._render_text(msg)},
            "at": {
                # atMobiles 要手机号；at_users 已由 dispatch 按渠道解析成可用手机号
                "atMobiles": list(msg.at_users),
                "isAtAll": msg.at_all,  # @所有人（注意：@全员会给人人发应用内/短信提醒，慎用）
            },
        }
        resp = await self._http.post(url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"钉钉推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("钉钉推送成功（%s）", msg.project_name)

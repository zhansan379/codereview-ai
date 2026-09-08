"""飞书（Lark）自定义机器人交互卡片推送 sink（reference/im_payloads.md §2）。

签名与钉钉不同：string_to_sign 作 HMAC **key**、对空字节串签名，timestamp 用**秒**，
签名放进 body（`timestamp` + `sign`），不是 URL 参数。正文用飞书 `lark_md`（不支持表格，
标题只支持 `**加粗**`），按评分给 header 着色。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

import httpx

from codereview_ai.notifiers.base import ReviewNotification, truncate_utf8

logger = logging.getLogger("codereview_ai.notifiers.feishu")


def _color_by_score(score: int | None) -> str:
    if score is None:
        return "blue"
    if score < 60:
        return "red"
    if score < 80:
        return "orange"
    return "green"


class FeishuNotifier:
    """飞书自定义机器人交互卡片推送；签名在每次 send 时计算。"""

    channel = "feishu"
    max_text_bytes = 30000

    def __init__(self, webhook: str, secret: str = "", *, http: httpx.AsyncClient) -> None:
        self._webhook = webhook
        self._secret = secret
        self._http = http

    def _sign(self) -> tuple[str, str]:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
        ).digest()
        return timestamp, base64.b64encode(hmac_code).decode("utf-8")

    def _render_card(self, msg: ReviewNotification) -> dict[str, object]:
        content_lines = [f"**项目**：{msg.project_name}", f"**MR/PR**：{msg.title}"]
        if msg.score is not None:
            content_lines.append(f"**总分**：{msg.score}")
        if msg.findings_count:
            parts = "、".join(f"{sev}×{n}" for sev, n in sorted(msg.findings_count.items()))
            content_lines.append(f"**发现**：{parts}")
        if msg.mention_names:
            content_lines.append(f"**相关**：{'、'.join(msg.mention_names)}")
        content_lines.append("")
        content_lines.append(truncate_utf8(msg.summary_md, self.max_text_bytes))
        # @ 人：卡片 lark_md 支持 <at user_id>；at_users 已由 dispatch 解析成 open_id
        for oid in msg.at_users:
            content_lines.append(f'<at user_id="{oid}">{oid}</at>')
        # @所有人：<at user_id="all"> 触发全员@（官方 bot 文档）
        if msg.at_all:
            content_lines.append('<at user_id="all">所有人</at>')
        body = "\n".join(content_lines)
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "代码审查报告"},
                "template": _color_by_score(msg.score),
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "查看 MR"},
                         "url": msg.url, "type": "primary"},
                    ],
                },
            ],
        }

    async def send(self, msg: ReviewNotification) -> None:
        payload: dict[str, object] = {
            "msg_type": "interactive",
            "card": self._render_card(msg),
        }
        if self._secret:
            ts, sign = self._sign()
            payload["timestamp"] = ts
            payload["sign"] = sign
        resp = await self._http.post(self._webhook, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"飞书推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("飞书推送成功（%s）", msg.project_name)

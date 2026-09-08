"""企业微信群机器人推送 sink（reference/im_payloads.md §3）。

企业微信机器人**不支持签名**（安全性靠 webhook key 保密）。content 上限 4096 字节，
超长截断 + 附查看完整报告链接；支持有限的 HTML 着色（`<font color="warning">`）。

**@ 说明**：企业微信机器人的 `markdown` 消息**不支持 @ 指定人**；只有 `text` 消息带
`mentioned_list`（userid，`"@all"` 表示 @所有人）或 `mentioned_mobile_list`（手机号）。
因此本 sink 双态：无需 @ → 发 markdown 富文本；需 @ → 改发 text 携带 @ 列表。
"""

from __future__ import annotations

import logging

import httpx

from codereview_ai.notifiers.base import ReviewNotification, truncate_utf8

logger = logging.getLogger("codereview_ai.notifiers.wecom")


class WeComNotifier:
    """企业微信群机器人推送；无签名，构造时注入 http 客户端。"""

    channel = "wecom"
    max_text_bytes = 4096

    def __init__(self, webhook: str, secret: str = "", *, http: httpx.AsyncClient) -> None:
        self._webhook = webhook
        self._http = http

    def _budget_body(self, msg: ReviewNotification, head: str, tail: str) -> str:
        """按「剩余预算」截断摘要，保住 head 与尾链；head/tail 不计入截断。"""
        fixed_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        budget = max(0, self.max_text_bytes - fixed_bytes)
        return truncate_utf8(msg.summary_md, budget) if budget > 0 else ""

    def _render_content(self, msg: ReviewNotification) -> str:
        """markdown 富文本路径（无 @ 时）；只把摘要按剩余预算截断。"""
        head = f"# 代码审查：{msg.title}\n"
        if msg.score is not None:
            head += f"> 总分 <font color=\"warning\">{msg.score}</font>\n"
        if msg.findings_count:
            parts = "、".join(f"{sev}×{n}" for sev, n in sorted(msg.findings_count.items()))
            head += f"> 发现 {parts}\n"
        tail = f"\n\n[查看完整报告]({msg.url})"
        return head + self._budget_body(msg, head, tail) + tail

    def _render_plain(self, msg: ReviewNotification) -> str:
        """text 路径正文（有 @ 时）；去除 markdown 语法，明文摘要。"""
        head = f"代码审查：{msg.title}\n"
        if msg.score is not None:
            head += f"总分：{msg.score}\n"
        if msg.findings_count:
            parts = "、".join(f"{sev}×{n}" for sev, n in sorted(msg.findings_count.items()))
            head += f"发现：{parts}\n"
        tail = f"\n\n查看完整报告：{msg.url}"
        return head + self._budget_body(msg, head, tail) + tail

    @staticmethod
    def _needs_mention(msg: ReviewNotification) -> bool:
        """有 @ 需求才切 text；@所有人 或 at_targets 里有可用 ID。"""
        if msg.at_all:
            return True
        return any(t.get("mobile") or t.get("wecom_userid") for t in msg.at_targets)

    def _mention_payload(self, msg: ReviewNotification) -> dict[str, object]:
        mentioned_list = [t["wecom_userid"] for t in msg.at_targets if t.get("wecom_userid")]
        mentioned_mobile_list = [t["mobile"] for t in msg.at_targets if t.get("mobile")]
        if msg.at_all:
            mentioned_list.append("@all")  # 企微 text 用 "@all" 表示 @所有人
        return {
            "msgtype": "text",
            "text": {
                "content": self._render_plain(msg),
                "mentioned_list": mentioned_list,
                "mentioned_mobile_list": mentioned_mobile_list,
            },
        }

    async def send(self, msg: ReviewNotification) -> None:
        payload: dict[str, object]
        if self._needs_mention(msg):
            payload = self._mention_payload(msg)  # text，带 @
        else:
            payload = {"msgtype": "markdown", "markdown": {"content": self._render_content(msg)}}
        resp = await self._http.post(self._webhook, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"企业微信推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("企业微信推送成功（%s）", msg.project_name)
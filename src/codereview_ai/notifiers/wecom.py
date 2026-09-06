"""企业微信群机器人 markdown 推送 sink（reference/im_payloads.md §3）。

企业微信机器人**不支持签名**（安全性靠 webhook key 保密）。content 上限 4096 字节，
超长截断 + 附查看完整报告链接；支持有限的 HTML 着色（`<font color="warning">`）。
"""

from __future__ import annotations

import logging

import httpx

from codereview_ai.notifiers.base import ReviewNotification, truncate_utf8

logger = logging.getLogger("codereview_ai.notifiers.wecom")


class WeComNotifier:
    """企业微信机器人 markdown 推送；无签名，构造时注入 http 客户端。"""

    channel = "wecom"
    max_text_bytes = 4096

    def __init__(self, webhook: str, secret: str = "", *, http: httpx.AsyncClient) -> None:
        self._webhook = webhook
        self._http = http

    def _render_content(self, msg: ReviewNotification) -> str:
        """组装 content；为保住「查看完整报告」链接，只把摘要部分按剩余预算截断。"""
        header = f"# 代码审查：{msg.title}\n"
        score_part = ""
        if msg.score is not None:
            score_part = f"> 总分 <font color=\"warning\">{msg.score}</font>\n"
        counts = ""
        if msg.findings_count:
            parts = "、".join(f"{sev}×{n}" for sev, n in sorted(msg.findings_count.items()))
            counts = f"> 发现 {parts}\n"
        link = f"\n\n[查看完整报告]({msg.url})"
        fixed_bytes = len(
            (header + score_part + counts).encode("utf-8")
        ) + len(link.encode("utf-8"))
        budget = max(0, self.max_text_bytes - fixed_bytes)
        body = truncate_utf8(msg.summary_md, budget) if budget > 0 else ""
        return f"{header}{score_part}{counts}{body}{link}"

    async def send(self, msg: ReviewNotification) -> None:
        payload: dict[str, object] = {
            "msgtype": "markdown",
            "markdown": {"content": self._render_content(msg)},
        }
        resp = await self._http.post(self._webhook, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"企业微信推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("企业微信推送成功（%s）", msg.project_name)

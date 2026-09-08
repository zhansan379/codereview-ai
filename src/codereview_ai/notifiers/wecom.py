"""企业微信群机器人推送 sink（reference/im_payloads.md §3）。

企业微信**不支持签名**（安全性靠 webhook key 保密）；content 上限 4096 字节，
超长截断 + 附查看完整报告链接；支持有限的 HTML 着色（`<font color="warning">`）。

**@ 成员**：企微群机器人支持真@——把 `at_users`（已按渠道解析成的 wecom userid）
以 `<@userid>` 扩展语法嵌进 content 即触发@提醒（官方文档 path/91770）；
`mention_names` 仍是文案点名（fork 用户名企微不认识，只作显示，不参与真@）。
"""

from __future__ import annotations

import logging

import httpx

from codereview_ai.notifiers.base import ReviewNotification, truncate_utf8

logger = logging.getLogger("codereview_ai.notifiers.wecom")


class WeComNotifier:
    """企业微信群机器人 markdown 推送；无签名，构造时注入 http 客户端。"""

    channel = "wecom"
    max_text_bytes = 4096

    def __init__(self, webhook: str, secret: str = "", *, http: httpx.AsyncClient) -> None:
        self._webhook = webhook
        self._http = http

    def _render_content(self, msg: ReviewNotification) -> str:
        """组装 content；为保住「查看完整报告」链接，只把摘要部分按剩余预算截断。

        `at_users` 是该渠道可用的 wecom userid（dispatch 解析），拼成 `<@userid>` 触发
        真@提醒；`mention_names`（fork 用户名）仅作正文点名、不发@。
        """
        header = f"# 代码审查：{msg.title}\n"
        score_part = ""
        if msg.score is not None:
            score_part = f"> 总分 <font color=\"warning\">{msg.score}</font>\n"
        counts = ""
        if msg.findings_count:
            parts = "、".join(f"{sev}×{n}" for sev, n in sorted(msg.findings_count.items()))
            counts = f"> 发现 {parts}\n"
        # 真@：<@userid> 扩展语法，userid 之间用空格分隔（官方 path/91770）
        at_line = ""
        if msg.at_users:
            at_line = " ".join(f"<@{u}>" for u in msg.at_users) + "\n"
        mentions = ""
        if msg.mention_names:
            mentions = f"> 相关：{'、'.join(msg.mention_names)}\n"
        link = f"\n\n[查看完整报告]({msg.url})"
        fixed_bytes = len(
            (header + score_part + counts + at_line + mentions).encode("utf-8")
        ) + len(link.encode("utf-8"))
        budget = max(0, self.max_text_bytes - fixed_bytes)
        body = truncate_utf8(msg.summary_md, budget) if budget > 0 else ""
        return f"{header}{score_part}{counts}{at_line}{mentions}{body}{link}"

    async def send(self, msg: ReviewNotification) -> None:
        payload: dict[str, object] = {
            "msgtype": "markdown",
            "markdown": {"content": self._render_content(msg)},
        }
        resp = await self._http.post(self._webhook, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"企业微信推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("企业微信推送成功（%s）", msg.project_name)
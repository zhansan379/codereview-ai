"""企业微信群机器人推送 sink（reference/im_payloads.md §3）。

企业微信**不支持签名**（安全性靠 webhook key 保密）；content 上限 4096 字节。

按 `render_v2` 分两档（官方 path/99110，能力互斥）：
- **markdown_v2**（render_v2=True，日报/汇总类）：支持表格/枚举/代码块等富格式，但
  **不支持 `<@>` 也 `不支持 <font color>`** ——无 @ 需求时用它，表格才能渲染。
- **markdown**（review 类）：支持 `<@userid>`/`<@all>` 真@ 与 `<font color>`，**无表格**。

**@ 成员**：markdown 支持真@——`at_users`（wecom userid）以 `<@userid>` 嵌进 content
即触发提醒（官方 path/91770）；`mention_names` 是文案点名（fork 用户名企微不认识）。
**@所有人**：markdown **没有** `mentioned_list` 字段（那是 text 的），`at_all` 时在 content
嵌 `<@all>`。markdown_v2 两种 @ 都不支持。

不再附「查看完整报告」链接（报告正文已含全量内容）。
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
        """组装 content，按 `render_v2` 选风味（官方 path/99110 能力互斥）：

        - v2（report 类）：只发标题 + 正文，无 @、无 `<font color>`、无链接——表格由企微
          端渲染。正文按剩余预算截断。
        - markdown（review 类）：`at_users`（wecom userid）拼 `<@userid>` 触发真@提醒，
          `at_all` 时嵌 `<@all>`；分数用 `<font color>`；`mention_names` 仅文案点名。
          同样截断正文，不附「查看完整报告」链接。
        """
        header = f"# {msg.title}\n"
        if getattr(msg, "render_v2", False):
            budget = max(0, self.max_text_bytes - len(header.encode("utf-8")))
            body = truncate_utf8(msg.summary_md, budget) if budget > 0 else ""
            return f"{header}{body}"
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
        # @所有人：markdown 无 mentioned_list；@all 同 `<@userid>` 一样写进 content 触发
        # （官方 path/91770）
        if msg.at_all:
            at_line += "<@all>\n"
        mentions = ""
        if msg.mention_names:
            mentions = f"> 相关：{'、'.join(msg.mention_names)}\n"
        fixed_bytes = len(
            (header + score_part + counts + at_line + mentions).encode("utf-8")
        )
        budget = max(0, self.max_text_bytes - fixed_bytes)
        body = truncate_utf8(msg.summary_md, budget) if budget > 0 else ""
        return f"{header}{score_part}{counts}{at_line}{mentions}{body}"

    async def send(self, msg: ReviewNotification) -> None:
        content = self._render_content(msg)
        if getattr(msg, "render_v2", False):
            # markdown_v2：支持表格，但无 @/字体颜色（官方 path/99110）
            v2: dict[str, object] = {"content": content}
            payload: dict[str, object] = {"msgtype": "markdown_v2", "markdown_v2": v2}
        else:
            # markdown：支持 <@userid>/<@all> 与 <font color>，无表格
            markdown: dict[str, object] = {"content": content}
            payload = {"msgtype": "markdown", "markdown": markdown}
        resp = await self._http.post(self._webhook, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"企业微信推送 http {resp.status_code}: {resp.text[:200]}")
        logger.info("企业微信推送成功（%s）", msg.project_name)

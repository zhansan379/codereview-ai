"""IM 通知推送（DESIGN F4 / reference/im_payloads.md）：中性通知模型 + 中性构建。

- `ReviewNotification`：平台无关的通知模型，由各渠道 Notifier 自行渲染成平台格式。
- `Notifier`（Protocol）：渠道 sink 抽象——负责 标准 markdown→平台方言、超长截断、
  签名、发送；发送只抛异常或成功，不会返回错误字符串。
- `build_review_notification`：把一次审查结果组装成中性通知。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from codereview_ai.domain.models import Finding, PullRequest, ReviewResult

#: 各渠道的超长截断上限（reference/im_payloads.md 的限制）
MAX_TEXT_BYTES: dict[str, int] = {
    "dingtalk": 20000,  # 钉钉 markdown text 上限约 20000 字节
    "feishu": 30000,  # 飞书卡片文本宽松，保守设大
    "wecom": 4096,  # 企业微信 content 上限 4096 字节（三家里最小）
}


@dataclass
class ReviewNotification:
    """一次审查结果的中性通知，供各渠道 Notifier 渲染。

    `at_users` 与 `mention_names` 语义分家：
    - `at_users`：**该平台认识的 @ID**（手机号/open_id/userid），由 dispatch 按渠道
      解析好再给 sink，sink 直接放进 at 字段即可——不再携带裸 fork username。
    - `mention_names`：**文案点名**（fork 用户名等平台不认识的标识），不受 @ 阈值
      门控，sink 渲染进正文，与真正的 @ 分开。
    """

    project_name: str
    title: str
    score: int | None
    summary_md: str
    url: str
    #: findings 明细（review 类通知填充；日报等 send_markdown 路径为空）。
    #: 各 sink 用 report_render.render_notification_body 渲染成渠道预算内的分点清单，
    #: 与 summary_md（通俗总结段落）拼成动态正文。
    findings: list[Finding] = field(default_factory=list)
    findings_count: dict[str, int] = field(default_factory=dict)
    #: 作者的**平台用户名**（GitHub login / Gitee 用户名等）——恒作为文案展示，
    #: 不依赖成员表解析；真 @ 提醒另行经 at_users（解析出平台 ID 才有，且受阈值门控）。
    author: str = ""
    source_branch: str = ""  # PR/MR 源分支；push 轨套壳无分支对（源=目标），sink 省略该行
    target_branch: str = ""
    at_users: list[str] = field(default_factory=list)  # 该渠道可用的 @ID
    mention_names: list[str] = field(default_factory=list)  # 文案点名（非门控）
    at_all: bool = False  # 评分低于阈值时是否 @全员（各渠道原生 @all）
    #: 企微「报告风味」：markdown_v2（支持表格，但**无 @、无字体颜色**）。
    #: review 类（要 @）不置位走 markdown；日报/汇总类（要表格、不带 @）置位。其他渠道忽略。
    render_v2: bool = False


class Notifier(Protocol):
    """IM 渠道 sink 抽象（reference/im_payloads.md §4）。"""

    channel: str
    max_text_bytes: int

    async def send(self, msg: ReviewNotification) -> None: ...


def build_review_notification(
    pr: PullRequest, result: ReviewResult
) -> ReviewNotification:
    """把一次审查结果组装成中性通知（供各 sink 渲染）。

    作者/分支作为元信息行展示：作者恒用平台用户名（不依赖成员表解析），真 @ 由
    dispatch 按渠道解析成员表后填充 `at_users`（解析出平台 ID 才 @，且受阈值门控）。
    作者不再进 `mention_names`（避免与作者行重复展示）；该字段留给其他文案点名。
    """
    by_sev: dict[str, int] = {}
    for f in result.findings:
        by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
    return ReviewNotification(
        project_name=pr.repo_full_name,
        title=f"#{pr.pr_number} {pr.title}".strip(),
        score=result.scores.total,
        summary_md=result.summary or "（无摘要）",
        url=pr.web_url,
        findings=list(result.findings),
        findings_count=by_sev,
        author=pr.author,
        source_branch=pr.source_branch,
        target_branch=pr.target_branch,
        mention_names=[],
    )


def truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节上限截断，且不切断多字节字符；超出时以省略号收尾。"""
    ellipsis = "…"
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    # 预留省略号（3 字节）空间；errors="ignore" 丢弃被削一半的多字节字符残片
    head = text.encode("utf-8")[: max_bytes - len(ellipsis.encode("utf-8"))].decode(
        "utf-8", errors="ignore"
    )
    return head.rstrip() + ellipsis

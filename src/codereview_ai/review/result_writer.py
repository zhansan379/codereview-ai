"""审查结果回写：总结评论 + 逐条行级评论（DESIGN §7.8 / reference §5）。

关键约束（reference §5.3）：**两个平台都只接受落在 diff hunk 范围内的行号**。
所以回写前必须用 `commentable_lines` 校验每个 finding 锚定的行号是否可评论，
越界/无法定位的（`line is None`）一律降级并入总结评论，而不是硬提交或丢弃。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable

import httpx

from codereview_ai.domain.models import FileDiff, Finding, PullRequest, ReviewResult
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.review.diffparse import CommentableLines, commentable_lines

logger = logging.getLogger("codereview_ai.review.result_writer")

#: severity → 展示符号（总结 Markdown 用）。
_SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


def partition_findings(
    findings: list[Finding],
    diffs: list[FileDiff],
) -> tuple[list[Finding], list[Finding]]:
    """把 findings 分成「可发行级评论」与「并入总结」。

    判据：行号落在对应文件的可评论集合里（RIGHT→新侧 / LEFT→旧侧）。
    """
    by_path: dict[str, CommentableLines] = {}
    for d in diffs:
        key = d.new_path or d.old_path
        if key:
            by_path[key] = commentable_lines(d.diff)
    inline: list[Finding] = []
    textual: list[Finding] = []
    for f in findings:
        cl = by_path.get(f.file)
        line = f.line if f.side == "RIGHT" else f.old_line
        if cl is not None and line is not None and cl.is_commentable(line, side=f.side):
            inline.append(f)
        else:
            textual.append(f)
    return inline, textual


def finding_to_comment(f: Finding) -> dict[str, object]:
    """把 Finding 转成 forge.post_inline 认识的评论 dict（side/line|old_line/body/path）。"""
    return {
        "side": f.side,
        "line": f.line,
        "old_line": f.old_line,
        "body": f.content,
        "path": f.file,
        "old_path": f.file,
    }


def build_summary_markdown(pr: PullRequest, textual: list[Finding], result: ReviewResult) -> str:
    """生成总结评论（含无法锚定的 findings 文本）。"""
    scores = result.scores
    lines = [
        f"🤖 AI 代码审查 · {pr.repo_full_name}#{pr.pr_number}",
        "",
        f"**总分 {scores.total} / 100**",
        "| 维度 | 得分 |",
        "|---|---|",
        f"| 正确性 | {scores.correctness}/40 |",
        f"| 安全 | {scores.security}/30 |",
        f"| 工程实践 | {scores.practices}/20 |",
        f"| 性能 | {scores.performance}/5 |",
        f"| 提交质量 | {scores.commit_quality}/5 |",
        "",
        result.summary,
    ]
    if textual:
        lines += [
            "",
            "---",
            "**无法定位到具体行、并入总结的建议：**",
            "",
        ]
        for f in textual:
            icon = _SEVERITY_ICON.get(str(f.severity), "⚪")
            lines.append(f"- {icon} **[{str(f.category)}] {f.file}**：{f.content}")
    if result.skipped_files:
        lines += [
            "",
            "---",
            "**被过滤、未审查的文件：**",
            *[f"- {p}" for p in result.skipped_files],
        ]
    return "\n".join(lines)


class ResultWriter:
    """把一个 ReviewResult 落到 forge 上：先行级评论，再总结评论。

    审查本身（LLM 调用、静态分析）昂贵且已随 `write` 前置完成；回写只是网络外发，
    偶发的连接级故障（`httpx.TransportError`，如本次的 `ConnectError`）不该把整个
    审查成果判死、白丢一次重算。故对回写做**指数退避重试**，且只重试网络层异常——
    `HTTPStatusError`（4xx/5xx）是平台侧真实拒绝，重试无意义，原样上抛。
    """

    def __init__(
        self,
        forge: ForgeAdapter,
        *,
        retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 30.0,
    ) -> None:
        self.forge = forge
        self._retries = max(0, int(retries))
        self._backoff_base = float(backoff_base)
        self._backoff_max = float(backoff_max)

    async def write(
        self,
        pr: PullRequest,
        diffs: list[FileDiff],
        result: ReviewResult,
        *,
        fingerprint: str = "",
        summary: str | None = None,
    ) -> None:
        """把一个 ReviewResult 落到 forge 上：先行级评论，再总结评论。

        `fingerprint` 给定时先做**幂等检查**：平台已含该指纹（上一轮重发已落地，或读超时
        歧义下评论已发出）→ 整体跳过，杜绝双发。指纹以 HTML 注释行 ``<!-- {fp} -->`` 附在
        总结末尾作唯一 sentinel（行级批量 + 总结各自是原子 POST，一个标志即够）。
        `summary` 给定时直接用（重发用已持久化的 `summary_md`，不再重新 build）。
        """
        if fingerprint and await self._already_delivered(pr, fingerprint):
            logger.info("回写跳过：评论已含指纹 %s（幂等命中）", fingerprint)
            return
        inline, textual = partition_findings(result.findings, diffs)
        summary_text = (
            summary if summary is not None else build_summary_markdown(pr, textual, result)
        )
        if fingerprint:
            summary_text = f"{summary_text}\n<!-- {fingerprint} -->"
        if inline:
            await self._post(
                lambda: self.forge.post_inline(pr, [finding_to_comment(f) for f in inline]),
                what="行级评论",
            )
        await self._post(
            lambda: self.forge.post_summary(pr, summary_text),
            what="总结评论",
        )

    async def _already_delivered(self, pr: PullRequest, fingerprint: str) -> bool:
        """平台是否已含该指纹，命中即视为本审查已投递（幂等跳过）。

        权宜守卫：极简/测试桩 forge 可能没有 `list_comments`（真实适配器一律继承
        基类默认返回空列表），此时无法幂等去重，降级为照发（与基类默认语义一致）。
        """
        list_comments = getattr(self.forge, "list_comments", None)
        if list_comments is None:
            return False
        marker = f"<!-- {fingerprint} -->"
        for body in await list_comments(pr):
            if marker in body:
                return True
        return False

    async def _post(self, fn: Callable[[], Awaitable[None]], *, what: str) -> None:
        """带指数退避重试执行一次 forge 回写；只对网络层异常重试。"""
        last: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                await fn()
                return
            except httpx.TransportError as exc:
                last = exc
                if attempt >= self._retries:
                    break
                delay = min(self._backoff_base * (2**attempt), self._backoff_max)
                logger.warning(
                    "回写%s遇网络异常，%.1fs 后重试（%d/%d）：%s",
                    what, delay, attempt + 1, self._retries, _describe_httpx(exc),
                )
                await asyncio.sleep(delay)
        if last is not None:
            # 重试耗尽：把可读信息带进异常，避免裸空消息的 httpx.ConnectError 无从排查
            raise RuntimeError(f"回写{what}多次失败：{_describe_httpx(last)}") from last


def _describe_httpx(exc: BaseException) -> str:
    """取可读错误文案：补齐 httpx 连接异常常见的空消息（空信息在底层 OSError 上）。"""
    detail = str(exc).strip()
    if not detail and exc.__cause__ is not None:
        detail = str(exc.__cause__).strip()
    cause = type(exc.__cause__).__name__ if exc.__cause__ else "?"
    return detail or f"{type(exc).__name__}({cause})"


def review_fingerprint(*, provider: str, repo_id: str, pr_number: int, head_sha: str) -> str:
    """同一份审查的稳定指纹：同 (provider, repo_id, pr_number, head_sha) 幂等可跨平台去重。"""
    raw = f"{provider}:{repo_id}:{pr_number}:{head_sha}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


async def redeliver(
    forge: ForgeAdapter,
    *,
    pr: PullRequest,
    findings: list[Finding],
    summary_md: str,
    fingerprint: str,
) -> None:
    """把**已持久化**的审查成果补发到平台；不重算（无 LLM 调用），只重取 diff + 回写。

    供「重新发送」按钮在首次回写失败（`writeback_failed`）后调用：从 DB 取回
    `findings` + `summary_md`，重建结果并走与正常回写同一套 `ResultWriter`
    （含指纹幂等 + 网络重试），点击幂等、重复触发不会在 PR 上双发评论。
    """
    refreshed = await forge.fetch_pull_request(pr)
    diffs = await forge.fetch_files(refreshed)
    result = ReviewResult(summary="", findings=list(findings))
    await ResultWriter(forge).write(
        refreshed, diffs, result,
        fingerprint=fingerprint, summary=summary_md,
    )

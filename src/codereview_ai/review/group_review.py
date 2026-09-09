"""语义分组接入主链（DESIGN §7.2 / §7.5，M4.6）：大变更分组并发审查 + 确定性合并。

- 大变更（`files ≥ GROUPING_MIN_FILES`）先 `SemanticGrouper.group()` 分组，否则一个整组。
- 每组一个独立 `Reviewer.review()`（组间 `asyncio.gather` 并发）。
- 合并（**确定性**）：findings union → 内容指纹去重 → 各维评分取最坏值，summary 拼接。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from codereview_ai.domain.models import FileDiff, Finding, PullRequest, ReviewResult
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.review.static_analysis import render_static_findings

#: 触发分组审查的文件数阈值；小于此数走整组一次审查（DESIGN §7.5「files≥4」）
GROUPING_MIN_FILES = 4


def _finding_fingerprint(file: str, content: str) -> str:
    """跨组 finding 的身份指纹：`file + ':' + content` 归一后哈希（与 increments 同思路）。"""
    body = (content or "").strip().lower()
    return hashlib.sha1(f"{file}:{body}".encode()).hexdigest()


def merge_results(results: list[ReviewResult], groups: list[list[FileDiff]]) -> ReviewResult:
    """把各组的 ReviewResult 确定性合并成一个整体结果。

    - findings：union 后按内容指纹去重（跨组同文件同问题的重复报只留一条）。
    - scores：逐维度取所有组的**最坏值**（不因分组稀释严重度）。
    - summary：按组序拼接，带组内文件清单前缀，便于定位。
    - skipped_files：取并集。
    """
    out = ReviewResult()
    seen: set[str] = set()
    for result, group in zip(results, groups, strict=True):
        out.skipped_files = sorted(set(out.skipped_files) | set(result.skipped_files))
        group_files = sorted({d.new_path for d in group})
        prefix = ",".join(group_files)
        if result.summary:
            out.summary += f"\n【{prefix}】{result.summary}".strip()
        for dim in ("correctness", "security", "practices", "performance", "commit_quality"):
            mine = getattr(result.scores, dim)
            current = getattr(out.scores, dim)
            setattr(out.scores, dim, max(current, mine))  # 取最坏值
        for f in result.findings:
            fp = _finding_fingerprint(f.file, f.content)
            if fp in seen:
                continue  # 跨组重复问题 → 去重
            seen.add(fp)
            out.findings.append(f)
    return out


def _attach_static(result: ReviewResult, static: list[Finding]) -> ReviewResult:
    """把静态 findings **硬写入**结果（DESIGN §11）。

    静态自带 `source='static:*'`，与 llm findings 天然不同源，直接 union 即可，
    不做跨源去重（同一问题工具与 LLM 分属两类信号，各自呈现）。
    """
    result.findings.extend(static)
    return result


async def review_in_groups(
    reviewer: Reviewer,
    grouper: SemanticGrouper | None,
    pr: PullRequest,
    commits_text: str,
    diffs: list[FileDiff],
    static_findings: list[Finding] | None = None,
    usage_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> ReviewResult:
    """把 diff 分组后每组独立审查，合并为整体结果；小变更整组一次。

    `grouper` 已包装好降级（LLM 失败 → per-file，见 grouping.py），主链可放心使用。
    `static_findings`（DESIGN §11）：每个分组只把**本组文件**的静态提示注入该组 prompt；
    最终把全部静态 findings 硬写入合并结果。
    `usage_sink`：透传给每组 `reviewer.review()`，diff 路径据此落 `ModelUsage`。
    """
    static = static_findings or []
    # fake reviewer 未必收 usage_sink → 仅在显式给定时才转发
    sink_kwargs = {"usage_sink": usage_sink} if usage_sink is not None else {}
    if grouper is None or len(diffs) < GROUPING_MIN_FILES:
        static_text = render_static_findings(static, {d.new_path for d in diffs})
        result = await reviewer.review(
            pr=pr, commits_text=commits_text, diffs=diffs,
            **_static_kwargs(static_text), **sink_kwargs,
        )
        return _attach_static(result, static)

    groups = await grouper.group(diffs)
    results = await asyncio.gather(*(
        reviewer.review(
            pr=pr, commits_text=commits_text, diffs=g,
            **_static_kwargs(render_static_findings(static, {d.new_path for d in g})),
            **sink_kwargs,
        )
        for g in groups
    ))
    merged = merge_results(list(results), groups)
    return _attach_static(merged, static)


def _static_kwargs(static_text: str) -> dict[str, str]:
    """空文本不改调用形状：缺省 fake reviewer（无 static 参数）也兼容。"""
    return {"static_findings_text": static_text} if static_text else {}

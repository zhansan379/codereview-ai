"""语义分组接入主链（DESIGN §7.2 / §7.5，M4.6）：大变更分组并发审查 + 确定性合并。

- 大变更（`files ≥ GROUPING_MIN_FILES`）先 `SemanticGrouper.group()` 分组，否则一个整组。
- 每组一个独立 `Reviewer.review()`（组间 `asyncio.gather` 并发）。
- 合并（**确定性**）：findings union → 内容指纹去重 → 各维评分取最坏值，summary 拼接。
"""

from __future__ import annotations

import asyncio
import hashlib

from codereview_ai.domain.models import FileDiff, PullRequest, ReviewResult
from codereview_ai.review.grouping import SemanticGrouper
from codereview_ai.review.reviewer import Reviewer

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


async def review_in_groups(
    reviewer: Reviewer,
    grouper: SemanticGrouper,
    pr: PullRequest,
    commits_text: str,
    diffs: list[FileDiff],
) -> ReviewResult:
    """把 diff 分组后每组独立审查，合并为整体结果；小变更整组一次。

    `grouper` 已包装好降级（LLM 失败 → per-file，见 grouping.py），主链可放心使用。
    """
    if len(diffs) < GROUPING_MIN_FILES:
        return await reviewer.review(pr=pr, commits_text=commits_text, diffs=diffs)

    groups = await grouper.group(diffs)
    results = await asyncio.gather(
        *(reviewer.review(pr=pr, commits_text=commits_text, diffs=g) for g in groups)
    )
    return merge_results(list(results), groups)

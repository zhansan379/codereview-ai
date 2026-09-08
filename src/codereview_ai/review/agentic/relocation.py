"""OCR re_location 钉行阶梯（照搬 resolver.go + relocation.go，main loop 后统一异步跑）。

OCR 在 code_comment 工具内联做 `ResolveComment→RelocateAcrossFiles→ReLocateComment(LLM)`
并最终 `ResolveLineNumbers` 回填。我们因 `ToolRunner.run_one` 是同步的（LLM 必须 async），
等价地把这条阶梯放到会话后统一重锚——语义一致，且正是 OCR 全局回填的后置版（计划已标注
此偏差）。

阶梯（对每个无 `line` 且带 `existing_code` 的 finding）：
1. 确定性：existing_code 的规范化片段在本文件新内容里匹配 → 填 `line`。
2. 跨文件：片段在全部 diff 的新内容里**唯一**命中 → 重钉 `file` + `line`（零/多命中拒绝）。
3. LLM 重定位：发 `RE_LOCATION_TASK`，取 fenced code block 替换 existing_code 重试匹配；
   失败还原。这是修"agentic 没标到行"的主战场。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from codereview_ai.domain.models import FileDiff, Finding
from codereview_ai.review.agentic.llmloop import AgentConfig, AgentLLM
from codereview_ai.review.agentic.prompts import (
    RE_LOCATION_TASK_SYSTEM,
    RE_LOCATION_TASK_USER,
)

logger = logging.getLogger("codereview_ai.agentic.relocation")

_FENCE_RE = re.compile(r"```(?:\w+)?\s*\n?([\s\S]*?)```")


def _norm_key(path: str) -> str:
    """文件路径规范化：去掉前导斜杠，与 diff_map 的 key 对齐（等 RepoContext）。"""
    return (path or "").removeprefix("/")


def _norm_signature(text: str) -> list[tuple[int, str]]:
    """把代码文本规范成「(1-based 原始行号, lstrip 后内容)」的非空行序列。

    逐行 `lstrip`（OCR 按行去前导空白匹配），跳过空行，但保留空行后的相对行距。
    返回的空行信息可推断匹配区间的行号。
    """
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(text.split("\n"), start=1):
        s = ln.rstrip("\n").lstrip()
        if s:
            out.append((i, s))
    return out


def _find_snippet(snippet: str, file_text: str) -> int | None:
    """返回 snippet 规范化后首次命中 file_text 的（新侧）起始行号；无命中返回 None。

    用非空行的规范化片段做**连续块**匹配（允许宿主空行穿插外），首个命中取起始行。
    """
    needle = [s for _i, s in _norm_signature(snippet)]
    if not needle:
        return None
    hay = _norm_signature(file_text)
    for start in range(len(hay) - len(needle) + 1):
        if all(hay[start + k][1] == needle[k] for k in range(len(needle))):
            return hay[start][0]
    return None


def _resolve_in_diff(f: Finding, d: FileDiff) -> bool:
    """确定性匹配：本文件新内容命中片段 → 填 line（新侧）。命中即视为已锚定。"""
    if not d.new_file_content:
        return False
    line_no = _find_snippet(f.existing_code, d.new_file_content)
    if line_no is None:
        return False
    f.file = _norm_key(d.new_path) or f.file
    f.line = line_no
    f.side = "RIGHT"
    return True


def _find_unique_across(diff_map: Mapping[str, FileDiff], snippet: str) -> tuple[str, int] | None:
    """在全部 diff 的新内容里找片段**唯一**命中；零/多命中返回 None（不猜）。"""
    hits: list[tuple[str, int]] = []
    for path, d in diff_map.items():
        if not d.new_file_content:
            continue
        ln = _find_snippet(snippet, d.new_file_content)
        if ln is not None:
            hits.append((_norm_key(path), ln))
    return hits[0] if len(hits) == 1 else None


def _relocate_across_files(f: Finding, diff_map: Mapping[str, FileDiff]) -> bool:
    """跨文件唯一串搜（声明/实现分离场景）：命中重钉 file + line。"""
    found = _find_unique_across(diff_map, f.existing_code)
    if found is None:
        return False
    f.file, f.line = found
    f.side = "RIGHT"
    logger.info("agentic 跨文件重锚 %s → line %s", f.file, f.line)
    return True


def _extract_fenced(text: str) -> str:
    """取首个 fenced code block 的内文；无 fence 回退整段 strip（OCR 亦宽松取块）。"""
    m = _FENCE_RE.search(text or "")
    if m:
        return m.group(1).strip("\n")
    return (text or "").strip()


async def _relocate_via_llm(llm: AgentLLM, f: Finding, d: FileDiff, cfg: AgentConfig) -> None:
    """LLM 重定位：发 RE_LOCATION_TASK，用返回片段替换 existing_code 重试匹配。

    失败还原原片段（OCR relocation.go:93 同规约），不抛异常。
    """
    try:
        user = RE_LOCATION_TASK_USER
        user = user.replace("{diff}", d.diff)
        user = user.replace("{existing_code}", f.existing_code or "")
        user = user.replace("{suggestion_content}", f.content)
        turn = await llm.chat(
            [{"role": "system", "content": RE_LOCATION_TASK_SYSTEM},
             {"role": "user", "content": user}],
            [],
        )
        block = _extract_fenced(str(getattr(turn, "content", None) or ""))
    except Exception as exc:  # noqa: BLE001 —— LLM 重定位失败不降级，仅回落总结
        logger.debug("agentic LLM 重定位失败：%s", exc)
        return
    if not block:
        return
    original = f.existing_code
    f.existing_code = block
    if _resolve_in_diff(f, d):
        logger.info("agentic LLM 重定位命中 %s → line %s", f.file, f.line)
    else:
        f.existing_code = original  # 失败还原，交由主链归总结


async def resolve_leaked_lines(
    comments: list[Finding],
    diff_map: Mapping[str, FileDiff],
    llm: AgentLLM,
    cfg: AgentConfig | None = None,
) -> list[Finding]:
    """对无 `line` 且带 `existing_code` 的 findings 跑钉行阶梯；其余原样保留。"""
    cfg = cfg or AgentConfig()
    for f in comments:
        if f.line is not None or not (f.existing_code or "").strip():
            continue  # 已锚定 / 无可锚定片段 → 留给总结
        # 1. 本文件确定性
        d = diff_map.get(_norm_key(f.file))
        if d is not None and _resolve_in_diff(f, d):
            continue
        # 2. 跨文件唯一串搜
        if _relocate_across_files(f, diff_map):
            d = diff_map.get(_norm_key(f.file))
            if d is not None and _resolve_in_diff(f, d):
                continue
        # 3. LLM 重定位（开关可关；d 欠本文件时跳过）
        if cfg.relocation_enabled and d is not None:
            await _relocate_via_llm(llm, f, d, cfg)
    return comments

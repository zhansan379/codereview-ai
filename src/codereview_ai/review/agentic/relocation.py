"""OCR re_location 钉行阶梯（照搬 resolver.go + relocation.go，main loop 后统一异步跑）。

OCR 在 code_comment 工具内联做 `ResolveComment→RelocateAcrossFiles→ReLocateComment(LLM)`
并最终 `ResolveLineNumbers` 回填。我们因 `ToolRunner.run_one` 是同步的（LLM 必须 async），
等价地把这条阶梯放到会话后统一重锚——语义一致，且正是 OCR 全局回填的后置版（计划已标注
此偏差）。

阶梯（对每个无 `line` 且带 `existing_code` 的 finding；模型不再自报行号，
`code_comment` schema 已不暴露 line，全部 finding 都走这条阶梯）：
1. 确定性（本文件）：hunk 优先——existing_code 先在 diff 新侧行序（context+added，
   行号由 `@@` 头推出）连续匹配，再旧侧行序，最后退到新文件全文扫描。
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
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _norm_key(path: str) -> str:
    """文件路径规范化：去掉前导斜杠，与 diff_map 的 key 对齐（等 RepoContext）。"""
    return (path or "").removeprefix("/")


def _norm_line(s: str) -> str:
    """单行规范化（= OCR diff.normalizeLine）：TrimSpace → 剥一个前导 +/- diff 标记 → TrimSpace。"""
    s = s.strip()
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        s = s[1:]
    return s.strip()


def _norm_signature(text: str) -> list[tuple[int, str]]:
    """把代码文本规范成「(1-based 原始行号, 规范化后内容)」的非空行序列。

    逐行 `_norm_line`（对齐 OCR：剥前导空白与 diff 标记），跳过空行，但保留空行后的
    相对行距。返回的行号可推断匹配区间的行号。
    """
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(text.split("\n"), start=1):
        s = _norm_line(ln)
        if s:
            out.append((i, s))
    return out


def _parse_hunk_sides(
    diff_text: str,
) -> list[tuple[list[tuple[int, str]], list[tuple[int, str]]]]:
    """解析 unified diff 自由文本 → 逐 hunk 的（新侧行序, 旧侧行序）。

    新侧 = context + added，行号从 `@@` 头新侧起点递增；旧侧 = context + deleted，
    行号从旧侧起点递增。行 = (1-based 行号, 规范化内容)，空行跳过。
    （= OCR resolver.go `extractSideLines`：同样两侧抽取/同号推进。）
    """
    hunks: list[tuple[list[tuple[int, str]], list[tuple[int, str]]]] = []
    new_lines: list[tuple[int, str]] | None = None
    old_lines: list[tuple[int, str]] | None = None
    old_no = new_no = 0
    for raw in (diff_text or "").split("\n"):
        m = _HUNK_HEADER_RE.match(raw)
        if m:
            if new_lines is not None and old_lines is not None:
                hunks.append((new_lines, old_lines))
            old_no, new_no = int(m.group(1)), int(m.group(3))
            new_lines, old_lines = [], []
            continue
        if new_lines is None or old_lines is None:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            s = _norm_line(raw[1:])
            if s:
                new_lines.append((new_no, s))
            new_no += 1
        elif raw.startswith("-"):
            s = _norm_line(raw[1:])
            if s:
                old_lines.append((old_no, s))
            old_no += 1
        else:
            content = raw[1:] if raw.startswith(" ") else raw
            s = _norm_line(content)
            if s:
                new_lines.append((new_no, s))
                old_lines.append((old_no, s))
            new_no += 1
            old_no += 1
    if new_lines is not None:
        hunks.append((new_lines, old_lines or []))
    return hunks


def _match_consecutive(
    side_lines: list[tuple[int, str]], needle: list[str]
) -> tuple[int, int] | None:
    """在「带行号的规范化行序列」里找 needle 的连续命中，返回 (起始行, 结束行)；无命中 None。

    结束行 = 命中块最后一条非空行的行号（= OCR matchConsecutive 的 StartLine/EndLine）。
    """
    if not needle or len(side_lines) < len(needle):
        return None
    for start in range(len(side_lines) - len(needle) + 1):
        if all(side_lines[start + k][1] == needle[k] for k in range(len(needle))):
            return side_lines[start][0], side_lines[start + len(needle) - 1][0]
    return None


def _resolve_from_hunk(f: Finding, d: FileDiff) -> bool:
    """hunk 优先锚定（= OCR resolver.go resolveFromHunk）：新侧行序先试，再旧侧。

    命中记区间（line, end_line 同侧）：与 OCR StartLine/EndLine 回填一致。
    """
    needle = [s for _i, s in _norm_signature(f.existing_code)]
    if not needle:
        return False
    hunks = _parse_hunk_sides(d.diff)
    for new_lines, _old_lines in hunks:  # 新侧：context + added → 新文件行号
        span = _match_consecutive(new_lines, needle)
        if span is not None:
            f.file = _norm_key(d.new_path) or f.file
            f.line, f.end_line = span
            f.side = "RIGHT"
            return True
    for _new_lines, old_lines in hunks:  # 旧侧：context + deleted → 旧文件行号
        span = _match_consecutive(old_lines, needle)
        if span is not None:
            f.file = _norm_key(d.new_path) or f.file
            f.old_line, f.end_line = span
            f.side = "LEFT"
            return True
    return False


def _find_snippet(snippet: str, file_text: str) -> tuple[int, int] | None:
    """返回 snippet 规范化后首次命中 file_text 的 (起始行, 结束行)；无命中返回 None。

    用非空行的规范化片段做**连续块**匹配（允许宿主空行穿插外），首个命中取整段区间。
    """
    needle = [s for _i, s in _norm_signature(snippet)]
    if not needle:
        return None
    hay = _norm_signature(file_text)
    for start in range(len(hay) - len(needle) + 1):
        if all(hay[start + k][1] == needle[k] for k in range(len(needle))):
            return hay[start][0], hay[start + len(needle) - 1][0]
    return None


def _resolve_in_diff(f: Finding, d: FileDiff) -> bool:
    """确定性锚定：hunk 优先（新侧→旧侧）→ 新文件全文扫描兜底。命中即已锚定。"""
    if _resolve_from_hunk(f, d):
        return True
    if not d.new_file_content:
        return False
    span = _find_snippet(f.existing_code, d.new_file_content)
    if span is None:
        return False
    f.file = _norm_key(d.new_path) or f.file
    f.line, f.end_line = span
    f.side = "RIGHT"
    return True


def _find_unique_across(
    diff_map: Mapping[str, FileDiff], snippet: str
) -> tuple[str, int, int] | None:
    """在全部 diff 的新内容里找片段**唯一**命中；零/多命中返回 None（不猜）。"""
    hits: list[tuple[str, int, int]] = []
    for path, d in diff_map.items():
        if not d.new_file_content:
            continue
        span = _find_snippet(snippet, d.new_file_content)
        if span is not None:
            hits.append((_norm_key(path), span[0], span[1]))
    return hits[0] if len(hits) == 1 else None


def _relocate_across_files(f: Finding, diff_map: Mapping[str, FileDiff]) -> bool:
    """跨文件唯一串搜（声明/实现分离场景）：命中重钉 file + line 区间。"""
    found = _find_unique_across(diff_map, f.existing_code)
    if found is None:
        return False
    f.file, f.line, f.end_line = found
    f.side = "RIGHT"
    logger.info("agentic 跨文件重锚 %s → line %s-%s", f.file, f.line, f.end_line)
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

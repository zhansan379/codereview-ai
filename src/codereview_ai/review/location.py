"""确定性行级定位（DESIGN §7.5）—— 从 OpenCodeReview (阿里) internal/diff/resolver.go 移植。

为什么不让 LLM 输出精确行号：行号在上下文里不稳定、会漂移。改为让 LLM 输出它
**看到的代码片段** `existing_code`，由工程做纯字符串匹配钉到真实行号。

定位顺序：
  1. hunk 内匹配：先在 diff hunks 里找连续匹配（新侧 → 旧侧）
  2. 全文匹配：hunk 没找到，扫描整个新文件内容（容忍空行）
  3. 跨文件迁移：评论片段其实属于另一个文件时（声明/实现拆分），唯一命中才迁

暴露给上层的是 `resolve_findings(findings, diffs)`：就地给每个 `Finding` 填入
`line/old_line/side`（并把跨文件迁移后的 file 写回），与领域模型直接对接。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codereview_ai.review.diffparse import Hunk, parse_hunks

if TYPE_CHECKING:
    from codereview_ai.domain.models import FileDiff, Finding


def _normalize_line(s: str) -> str:
    s = s.strip()
    s = s[1:] if s.startswith(("+", "-")) else s
    return s.strip()


def _split_normalize(code: str) -> list[str]:
    """把代码片段按行拆分并归一化，丢弃空行。"""
    out = []
    for raw in code.split("\n"):
        n = _normalize_line(raw)
        if n:
            out.append(n)
    return out


@dataclass
class LocatedComment:
    path: str
    existing_code: str
    start_line: int = 0
    end_line: int = 0
    old_line: int | None = None
    new_line: int | None = None
    side: str = "RIGHT"  # "RIGHT"=新侧 | "LEFT"=旧侧


def _extract_side_lines(hunk: Hunk, new_side: bool) -> list[tuple[int, str]]:
    """取 hunk 的一侧（带行号的非删/增行序列）。

    new_side=True  → 上下文+新增行，行号是新文件行号
    new_side=False → 上下文+删除行，行号是旧文件行号
    """
    result = []
    old_line, new_line = hunk.old_start, hunk.new_start
    for ln in hunk.lines:
        if ln.type == " ":
            result.append((new_line if new_side else old_line, _normalize_line(ln.content)))
            old_line += 1
            new_line += 1
        elif ln.type == "+":
            if new_side:
                result.append((new_line, _normalize_line(ln.content)))
            new_line += 1
        elif ln.type == "-":
            if not new_side:
                result.append((old_line, _normalize_line(ln.content)))
            old_line += 1
    return result


def _match_consecutive(side_lines: list[tuple[int, str]], target: list[str]) -> tuple[int, int]:
    """一段**连续**且全部匹配 target 的行，返回 (start,end)（(0,0) 表示未命中）。"""
    if not target or len(side_lines) < len(target):
        return 0, 0
    for i in range(len(side_lines) - len(target) + 1):
        if all(side_lines[i + j][1] == t for j, t in enumerate(target)):
            return side_lines[i][0], side_lines[i + len(target) - 1][0]
    return 0, 0


def _resolve_from_hunk(d: FileDiff, cm: LocatedComment) -> bool:
    hunks = parse_hunks(d.diff)
    if not hunks:
        return False
    target = _split_normalize(cm.existing_code)
    if not target:
        return False
    # 先新侧再旧侧：必须记住命中侧，回写层才能拿到 old_line/new_line/side
    for h in hunks:
        s, e = _match_consecutive(_extract_side_lines(h, True), target)
        if s and e:
            cm.start_line, cm.end_line = s, e
            cm.side, cm.new_line, cm.old_line = "RIGHT", s, None
            return True
    for h in hunks:
        s, e = _match_consecutive(_extract_side_lines(h, False), target)
        if s and e:
            cm.start_line, cm.end_line = s, e
            cm.side, cm.old_line, cm.new_line = "LEFT", s, None
            return True
    return False


def _resolve_from_file_content(d: FileDiff, cm: LocatedComment) -> bool:
    if not d.new_file_content:
        return False
    target = _split_normalize(cm.existing_code)
    if not target:
        return False
    # 全文：跳过空行再做连续匹配（空行不参与窗口）
    norm, nums = [], []
    for i, line in enumerate(d.new_file_content.split("\n"), start=1):
        n = _normalize_line(line.rstrip("\r"))
        if n:
            norm.append(n)
            nums.append(i)
    if len(norm) < len(target):
        return False
    for i in range(len(norm) - len(target) + 1):
        if all(norm[i + j] == t for j, t in enumerate(target)):
            cm.start_line, cm.end_line = nums[i], nums[i + len(target) - 1]
            # 全文兜底只扫新文件内容，一律命中新侧
            cm.side, cm.new_line, cm.old_line = "RIGHT", cm.start_line, None
            return True
    return False


def resolve_comment(cm: LocatedComment, d: FileDiff) -> bool:
    """先 hunk 内，再全文。成功返回 True，cm 上填入真实行号。"""
    if cm.start_line > 0 or cm.end_line > 0 or not cm.existing_code:
        return False
    return _resolve_from_hunk(d, cm) or _resolve_from_file_content(d, cm)


def relocate_across_files(cm: LocatedComment, diffs: list[FileDiff]) -> tuple[str, bool]:
    """跨文件迁移：existing_code 真正属于另一个文件。唯一命中才迁移；
    0 命中或 ≥2 命中都放弃（样板代码可能合法出现在多个文件，猜一个不如不猜）。

    返回 (新 path, 是否迁移)。迁移时 cm 的 path 与侧向行号一起改，保证回写层
    拿到的还是目标文件正确的 side 与行号，而不是源文件的。
    """
    if not cm.existing_code or not diffs:
        return "", False
    hits: list[tuple[str, LocatedComment]] = []
    for d in diffs:
        if d.new_path == cm.path or d.old_path == cm.path:
            continue
        probe = replace(cm, start_line=0, end_line=0, old_line=None, new_line=None, side="RIGHT")
        if not resolve_comment(probe, d):
            continue
        path = d.new_path or d.old_path
        hits.append((path, probe))
        if len(hits) > 1:
            return "", False  # 已歧义，不再判定
    if len(hits) != 1:
        return "", False
    found_path, found = hits[0]
    cm.path = found_path
    cm.start_line, cm.end_line = found.start_line, found.end_line
    cm.side, cm.new_line, cm.old_line = found.side, found.new_line, found.old_line
    return cm.path, True


def resolve_line_numbers(
    comments: list[LocatedComment], diffs: list[FileDiff]
) -> list[LocatedComment]:
    """对无行号的评论做 hunk/全文定位，失败时尝试跨文件迁移。

    仍无法定位的（start_line 仍为 0），由调用方决定降级并入总结评论。
    """
    by_path: dict[str, FileDiff] = {}
    for diff in diffs:
        if diff.new_path and diff.new_path != "/dev/null":
            by_path[diff.new_path] = diff
        if diff.old_path and diff.old_path != "/dev/null":
            by_path.setdefault(diff.old_path, diff)
    result = []
    for cm in comments:
        if cm.start_line > 0 or cm.end_line > 0 or not cm.existing_code:
            result.append(cm)
            continue
        d = by_path.get(cm.path)
        if d is None:
            result.append(cm)
            continue
        if not resolve_comment(cm, d):
            relocate_across_files(cm, diffs)
        result.append(cm)
    return result


# ---------------------------------------------------------------------------
# 领域模型桥：Finding（LLM 产出） ↔ LocatedComment（定位）
# ---------------------------------------------------------------------------


def _by_path(diffs: list[FileDiff]) -> dict[str, FileDiff]:
    by_path: dict[str, FileDiff] = {}
    for d in diffs:
        if d.new_path and d.new_path != "/dev/null":
            by_path[d.new_path] = d
        if d.old_path and d.old_path != "/dev/null":
            by_path.setdefault(d.old_path, d)
    return by_path


def resolve_findings(findings: list[Finding], diffs: list[FileDiff]) -> list[Finding]:
    """就地给 `Finding` 填入锚定结果（line/old_line/side），失败尝试跨文件迁移。

    迁移成功会改 `f.file`。仍无法定位的（line 为 None），调用方决定降级。
    """
    by_path = _by_path(diffs)
    for f in findings:
        if f.line is not None or not f.existing_code:
            continue
        cm = LocatedComment(path=f.file, existing_code=f.existing_code)
        d = by_path.get(f.file)
        if d is not None and resolve_comment(cm, d):
            _writeback(f, cm)
            continue
        moved, ok = relocate_across_files(cm, diffs)
        if ok:
            f.file = moved
            _writeback(f, cm)
    return findings


def _writeback(f: Finding, cm: LocatedComment) -> None:
    f.line = cm.start_line
    f.old_line = cm.old_line
    f.side = cm.side

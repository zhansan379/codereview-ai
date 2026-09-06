"""确定性行级定位 —— 从 OpenCodeReview (阿里) internal/diff/resolver.go 移植的 Python 版。

为什么是它：OCR 不让 LLM 输出精确行号（行号在上下文里极不稳定，会漂移），
而是让 LLM 输出它**看到的代码片段** `existing_code`，然后用这个片段在 diff 里
做纯字符串匹配定位真实行号。位置由工程逻辑保证，不是由模型猜测。

定位顺序（1:1 复刻 resolver.go）：
  1. hunk 内匹配：先在 diff hunks 里找 consecutive 匹配（新侧→旧侧）
  2. 全文匹配：hunk 没找到，扫描整个新文件内容（容忍空行），逐行匹配
  3. 跨文件迁移（RelocateAcrossFiles）：评论的片段其实属于另一个文件时
     （声明/实现拆分常见），纯字符串匹配把它迁移到真正所属文件，唯一命中才迁

匹配细节（复刻 normalizeLine / splitAndNormalize）：
  - 每行 TrimSpace + 剥离前缀 '+'/'-'（diff 标记）再 TrimSpace
  - 只匹配**连续**的非空行序列（空行不参与，避免空行破坏滑动窗口）

用法：
  hunks = parse_hunks(file_diff.diff)         # diff → Hunk 结构
  cm = {"path": "...", "existing_code": "..."}
  if not resolve_comment(cm, file_diff):       # 在 hunk/全文里定位
      relocate_across_files(cm, all_diffs)     # 定位失败可能是文件不对
  定位成功 → cm 上的 start_line/end_line 是命中所在一侧的行号，且额外带 old_line/
  new_line 与 side/edit_type：GitLab 回写用 old_line（deleted）或 new_line；
  GitHub 用 side:LEFT+old_line 或 side:RIGHT+new_line。不再需要"回忆命中的是哪一侧"。

可放到新项目 src/codereview_ai/review/location.py。
纯标准库，无第三方依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace


# ---------------------------------------------------------------------------
# 1. unified diff 解析（移植 internal/diff/hunk.go 的 ParseHunks）
# ---------------------------------------------------------------------------

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class HunkLine:
    type: str          # "+" 新增 | "-" 删除 | " " 上下文
    content: str


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    diff: str                     # 该文件的 unified diff 文本
    new_file_content: str = ""    # 定位兜底用的新文件全文


def parse_hunks(raw_diff_text: str) -> list[Hunk]:
    """把一个文件的 unified diff 文本解析为 hunks 列表。"""
    hunks: list[Hunk] = []
    current: Hunk | None = None
    for line in raw_diff_text.split("\n"):
        m = HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                hunks.append(current)
            current = Hunk(
                old_start=int(m.group(1)),
                old_count=int(m.group(2)) if m.group(2) else 1,
                new_start=int(m.group(3)),
                new_count=int(m.group(4)) if m.group(4) else 1,
            )
            continue
        if current is None:
            continue  # 跳过 "diff --git" / "---" / "+++" 等文件级头
        if line.startswith("\\ No newline at end of file") or line.startswith("diff --git "):
            continue
        if line.startswith("+"):
            current.lines.append(HunkLine("+", line[1:]))
        elif line.startswith("-"):
            current.lines.append(HunkLine("-", line[1:]))
        else:
            content = line[1:] if line.startswith(" ") else line
            current.lines.append(HunkLine(" ", content))
    if current is not None:
        hunks.append(current)
    return hunks


# ---------------------------------------------------------------------------
# 2. 行归一化（移植 normalizeLine / splitAndNormalize）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 3. 定位主逻辑（移植 ResolveComment / ResolveLineNumbers）
# ---------------------------------------------------------------------------

@dataclass
class LocatedComment:
    path: str
    existing_code: str
    start_line: int = 0           # 命中所在一侧的首行（可能是旧侧或新侧）
    end_line: int = 0             # 命中所在一侧的末行
    old_line: int | None = None   # 命中在旧侧时 = start_line..end_line；否则 None
    new_line: int | None = None   # 命中在新侧时 = start_line..end_line；否则 None
    side: str = "RIGHT"           # "RIGHT"=新侧 / "LEFT"=旧侧；派生给 GitHub 的 side

    @property
    def edit_type(self) -> str:
        """派生自 side：旧侧=deleted（GitLab 用 old_line、GitHub 用 side:LEFT），
        新侧=added（写入时用 new_line/side:RIGHT）。本属性只保证回写必需位
        ——是否删除行；需进一步区分 added/modified 时由调用方结合命中行构成判断。"""
        return "deleted" if self.side == "LEFT" else "added"


def _extract_side_lines(hunk: Hunk, new_side: bool) -> list[tuple[int, str]]:
    """取 hunk 的一侧（带行号的非删/增行序列）。

    new_side=True  → 上下文+新增行，行号是新文件行号
    new_side=False → 上下文+删除行，行号是旧文件行号
    """
    result = []
    old_line, new_line = hunk.old_start, hunk.new_start
    for l in hunk.lines:
        if l.type == " ":  # 上下文
            result.append((new_line if new_side else old_line, _normalize_line(l.content)))
            old_line += 1
            new_line += 1
        elif l.type == "+":
            if new_side:
                result.append((new_line, _normalize_line(l.content)))
            new_line += 1
        elif l.type == "-":
            if not new_side:
                result.append((old_line, _normalize_line(l.content)))
            old_line += 1
    return result


def _match_consecutive(side_lines: list[tuple[int, str]], target: list[str]) -> tuple[int, int]:
    """在带行号的序列里找一段**连续**且全部匹配 target 的行，返回 (start,end)（0,0 表示未命中）。"""
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
    # 先新侧，再旧侧：必须记住命中侧，回写层才能拿到 old_line/new_line/side
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
    """跨文件迁移：existing_code 真正属于另一个文件（声明/实现拆分场景）。

    纯字符串匹配、不调模型。唯一命中才迁移；0 命中或 ≥2 命中都放弃，
    因为同一个样板代码可能合法出现在多个文件，猜一个不如不猜。

    返回 (新 path, 是否迁移)。迁移时 cm 的 path 与侧向行号(side/old_line/new_line)
    一起改，保证回写层拿到的还是目标文件正确的 side 与行号，而不是源文件的。
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


# ---------------------------------------------------------------------------
# 4. 端到端：一组评论 → 全部定位
# ---------------------------------------------------------------------------

def resolve_line_numbers(comments: list[LocatedComment], diffs: list[FileDiff]) -> list[LocatedComment]:
    """1:1 复刻 ResolveLineNumbers：对所有无行号的评论做 hunk/全文定位。

    调用方接在 LLM 结构化输出之后：LLM 给每个 finding 的 existing_code，
    这里统一把它们钉到真实行号。仍无法定位的（line 仍为 0），调用方决定
    降级并入总结评论（参考 codereview-ai DESIGN 的行级评论章节）。
    """
    by_path: dict[str, FileDiff] = {}
    for d in diffs:
        if d.new_path and d.new_path != "/dev/null":
            by_path[d.new_path] = d
        if d.old_path and d.old_path != "/dev/null":
            by_path.setdefault(d.old_path, d)
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
            relocate_across_files(cm, diffs)   # 失败可能是文件不对
        result.append(cm)
    return result
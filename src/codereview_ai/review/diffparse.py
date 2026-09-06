"""unified diff 解析 + 可评论行集合（DESIGN §7.1）。

把单个文件的 unified diff 解析成 hunks，并据此计算**可评论行集合**：
- 新侧：上下文行 + 新增行（行号为新文件行号）
- 旧侧：上下文行 + 删除行（行号为旧文件行号）

可评论行集合是行级评论正确性的地基（DESIGN §8/§13.4）：回写前必须校验
finding 锚定的行号落在对应文件的可评论集合里，越界即降级入总结评论。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class HunkLine:
    type: str  # "+" 新增 | "-" 删除 | " " 上下文
    content: str


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)


def parse_hunks(raw_diff_text: str) -> list[Hunk]:
    """把一个文件的 unified diff 文本解析为 hunks 列表。

    - `@@ -1,5 +1,6 @@` 头（缺 count 时默认 1）
    - 跳过 `diff --git` / `---` / `+++` 等文件级头
    - 忽略 `\\ No newline at end of file` 标记
    """
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
            continue
        if not line:
            continue  # 尾部换行产生的虚拟空行，非真实内容；真实空上下文行是 " "
        if line.startswith("\\") or line.startswith("diff --git "):
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


@dataclass(frozen=True)
class CommentableLines:
    """一个文件的可评论行号集合（用 frozenset 保证不可变、可哈希）。"""

    new: frozenset[int]  # 新侧：上下文 + 新增
    old: frozenset[int]  # 旧侧：上下文 + 删除

    def is_commentable(self, line: int, *, side: str = "RIGHT") -> bool:
        return line in (self.new if side == "RIGHT" else self.old)


def commentable_lines(raw_diff_text: str) -> CommentableLines:
    """由一文件的 diff 文本计算可评论行集合。"""
    new: set[int] = set()
    old: set[int] = set()
    for h in parse_hunks(raw_diff_text):
        ol, nl = h.old_start, h.new_start
        for ln in h.lines:
            if ln.type == " ":
                new.add(nl)
                old.add(ol)
                ol += 1
                nl += 1
            elif ln.type == "+":
                new.add(nl)
                nl += 1
            elif ln.type == "-":
                old.add(ol)
                ol += 1
    return CommentableLines(frozenset(new), frozenset(old))

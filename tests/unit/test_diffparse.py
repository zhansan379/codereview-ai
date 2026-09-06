"""review/diffparse 测试：unified diff 解析 + 可评论行集合。

注意：真实 unified diff 里空上下文行是单个空格 ` `，而非空串；空串是尾部
换号产生的虚拟行（解析器会跳过）。
"""

from __future__ import annotations

from codereview_ai.review.diffparse import commentable_lines, parse_hunks

SAMPLE = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,5 +1,6 @@\n"
    " def greet(name):\n"
    "     return f\"hi {name}\"\n"
    " \n"
    "+def byebye(name):\n"
    "+    return f\"bye {name}\"\n"
    "+\n"
    " def unused():\n"
    "     pass\n"
)


def test_parse_hunks_counts_and_lines():
    hunks = parse_hunks(SAMPLE)
    assert len(hunks) == 1
    h = hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (1, 5, 1, 6)
    types = [ln.type for ln in h.lines]
    assert types == [" ", " ", " ", "+", "+", "+", " ", " "]
    # 文件级头与尾随虚拟空行被跳过
    assert all(not ln.content.startswith("diff --git") for ln in h.lines)


def test_parse_hunks_omitted_count_defaults_to_one():
    hunks = parse_hunks("@@ -1 +2 @@\n-a\n+b\n")
    assert len(hunks) == 1
    h = hunks[0]
    assert (h.old_count, h.new_count) == (1, 1)


def test_commentable_lines_new_side():
    cl = commentable_lines(SAMPLE)
    # 新侧：上下文(1,2,3,7,8) + 新增(4,5,6)
    assert sorted(cl.new) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert cl.is_commentable(4, side="RIGHT")
    assert cl.is_commentable(8, side="RIGHT")
    assert cl.is_commentable(3, side="RIGHT")
    assert not cl.is_commentable(99, side="RIGHT")


def test_commentable_lines_old_side_of_deletions():
    cl = commentable_lines("@@ -2,3 +1,1 @@\n keep\n-old\n-gone\n new\n")
    # 旧侧：上下文(2) + 删除(3,4) + 上下文(5)
    # 新侧：上下文(1) + 上下文(2)
    assert sorted(cl.old) == [2, 3, 4, 5]
    assert sorted(cl.new) == [1, 2]
    assert cl.is_commentable(3, side="LEFT")
    assert cl.is_commentable(2, side="LEFT")
    assert not cl.is_commentable(3, side="RIGHT")


def test_empty_diff_gives_empty_sets():
    cl = commentable_lines("")
    assert cl.new == frozenset()
    assert cl.old == frozenset()
    assert not cl.is_commentable(1)


def test_trailing_newline_virtual_line_is_skipped():
    # 末尾硬换号产生的 "" 不应计入
    cl = commentable_lines("@@ -1,1 +1,2 @@\n a\n+b\n\n")
    assert sorted(cl.new) == [1, 2]
    assert sorted(cl.old) == [1]

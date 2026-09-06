"""review/location 测试：hunk 内匹配、全文兜底、跨文件迁移、Finding 写回。

这是 M2 的核心风险（DESIGN §19：锚定定位命中率 ≥85%），必须有精准的落地断言。

fixture 约定：
- app.py  ：新增了 `def live():`（新增行）＋ 若干上下文行
- impl.py ：新增了 `def byebye(name):`（仅此文件含 byebye）
"""

from __future__ import annotations

from codereview_ai.domain.models import Category, ChangeType, FileDiff, Finding, Severity
from codereview_ai.review.location import (
    LocatedComment,
    relocate_across_files,
    resolve_comment,
    resolve_findings,
)

APP_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,5 +1,6 @@\n"
    " def greet(name):\n"
    "     return f\"hi {name}\"\n"
    " \n"
    "+def live():\n"
    "+    return 1\n"
    "+\n"
    " def old_fn():\n"
    "     do_something()\n"
)

APP_CONTENT = (
    "def greet(name):\n"
    "    return f\"hi {name}\"\n"
    "\n"
    "def live():\n"
    "    return 1\n"
    "\n"
    "def old_fn():\n"
    "    do_something()\n"
    "    MAGIC = 42\n"  # 仅供跨文件歧义测试（不出现在 diff，走全文兜底）
)

IMPL_DIFF = (
    "diff --git a/impl.py b/impl.py\n"
    "--- a/impl.py\n"
    "+++ b/impl.py\n"
    "@@ -1,2 +1,5 @@\n"
    " def helper():\n"
    "     return 0\n"
    " \n"
    "+def byebye(name):\n"
    "+    return f\"bye {name}\"\n"
)

IMPL_CONTENT = (
    "def helper():\n"
    "    return 0\n"
    "\n"
    "def byebye(name):\n"
    "    return f\"bye {name}\"\n"
    "    MAGIC = 42\n"
)

DELETION_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -5,4 +5,2 @@
 def removed():
-    old_body_1
-    old_body_2
 def kept():
"""


def app_diff() -> FileDiff:
    return FileDiff("app.py", "app.py", APP_DIFF, 3, 0, ChangeType.MODIFIED, APP_CONTENT)


def impl_diff() -> FileDiff:
    return FileDiff("impl.py", "impl.py", IMPL_DIFF, 3, 0, ChangeType.MODIFIED, IMPL_CONTENT)


def deletion_diff() -> FileDiff:
    return FileDiff("app.py", "app.py", DELETION_DIFF, 0, 2, ChangeType.MODIFIED,
                    "def removed():\ndef kept():\n")


def _finding(existing: str, file: str = "app.py") -> Finding:
    return Finding(
        content="x",
        category=Category.BUG,
        severity=Severity.MEDIUM,
        existing_code=existing,
        file=file,
    )


def test_hunk_match_new_side_added_line():
    cm = LocatedComment(path="impl.py", existing_code="def byebye(name):")
    assert resolve_comment(cm, impl_diff()) is True
    assert cm.side == "RIGHT"
    assert cm.new_line == 4
    assert cm.old_line is None


def test_hunk_match_old_side_deleted_line():
    cm = LocatedComment(path="app.py", existing_code="    old_body_1")
    assert resolve_comment(cm, deletion_diff()) is True
    assert cm.side == "LEFT"
    assert cm.old_line == 6
    assert cm.new_line is None


def test_hunk_match_new_side_context():
    cm = LocatedComment(path="app.py", existing_code="def greet(name):")
    assert resolve_comment(cm, app_diff()) is True
    assert cm.side == "RIGHT"
    assert cm.new_line == 1


def test_hunk_match_new_side_context_tail():
    cm = LocatedComment(path="app.py", existing_code="    do_something()")
    assert resolve_comment(cm, app_diff()) is True
    assert cm.side == "RIGHT"
    assert cm.new_line == 8


def test_relocate_across_files_unique_only():
    cm = LocatedComment(path="stub.py", existing_code="def byebye(name):")
    moved, ok = relocate_across_files(cm, [app_diff(), impl_diff()])
    assert ok is True and moved == "impl.py"
    assert cm.side == "RIGHT" and cm.new_line == 4


def test_relocate_gives_up_on_ambiguity():
    cm = LocatedComment(path="stub.py", existing_code="    MAGIC = 42")
    # MAGIC 在 app.py 与 impl.py 全文兜底都能命中 → 歧义，丢弃不猜
    moved, ok = relocate_across_files(cm, [app_diff(), impl_diff()])
    assert (moved, ok) == ("", False)


def test_unlocatable_stays_line_none():
    findings = [_finding("def vanished_entirely():")]
    resolve_findings(findings, [app_diff()])
    assert findings[0].line is None


def test_resolve_findings_writeback_and_relocate():
    findings = [
        _finding("def greet(name):"),                  # app.py 命中
        _finding("def byebye(name):", file="stub.py"),  # 唯一命中 impl.py → 迁移
    ]
    resolve_findings(findings, [app_diff(), impl_diff()])
    assert findings[0].line == 1 and findings[0].side == "RIGHT"
    assert findings[1].file == "impl.py"
    assert findings[1].line == 4 and findings[1].side == "RIGHT"


def test_resolve_findings_skips_already_located():
    f = _finding("def greet(name):")
    f.line = 99  # 已有行号则不再重定位
    resolve_findings([f], [app_diff()])
    assert f.line == 99

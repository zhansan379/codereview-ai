"""静态分析融合（DESIGN §11）单测：ruff/semgrep 归一化 + prompt 注入 + 硬写入 + 降级。

离线：注入 `FakeStaticRunner` 返回罐头 JSON；临时目录物化工作区；fake Reviewer 断言
prompt 含 static_findings_text 且静态 findings 进 result。
"""

from __future__ import annotations

import json

from codereview_ai.domain.models import Category, ChangeType, FileDiff, Severity
from codereview_ai.review.group_review import review_in_groups
from codereview_ai.review.reviewer import Reviewer, ReviewerConfig, build_messages
from codereview_ai.review.static_analysis import (
    RunResult,
    StaticAnalyzer,
    render_static_findings,
)


def _py_diff(content: str, path: str = "a.py") -> FileDiff:
    return FileDiff(
        old_path=path, new_path=path,
        diff=f"---\n+++\n@@ +1 +3 @@\n+{content}",
        additions=1, deletions=0, change_type=ChangeType.MODIFIED,
        new_file_content=content,
    )


def _json(content: str) -> FileDiff:
    return FileDiff(
        old_path="a.js", new_path="a.js",
        diff="---\n+++\n@@ +1 +1 @@\n+x",
        additions=1, deletions=0, change_type=ChangeType.MODIFIED,
        new_file_content="x\n",
    )


class _FakeRunner:
    """离线工具执行器：按 tool 返回罐头 JSON（ruff 列表 / semgrep 对象）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_oserror = False

    async def run(self, tool: str, args: list[str], cwd) -> RunResult:
        self.calls.append(tool)
        if self.raise_oserror:
            raise OSError("工具未安装")
        if tool == "ruff":
            return RunResult(json.dumps([
                {
                    "code": "F401",
                    "message": "'os' imported but unused",
                    "filename": "a.py",
                    "location": {"row": 3, "column": 5, "end_location": {"row": 3, "column": 12}},
                },
            ]), 1)
        # semgrep
        return RunResult(json.dumps({
            "results": [
                {
                    "check_id": "python.lang.security.audit.eval-usage",
                    "path": "a.py",
                    "start": {"line": 7, "col": 1},
                    "end": {"line": 7, "col": 20},
                    "extra": {"severity": "ERROR", "message": "eval 可用于任意代码执行",
                             "fix": None},
                }
            ],
            "errors": [],
        }), 0)


class _EchoReviewer(Reviewer):
    """记录收到的消息、返回空 findings 的假 Reviewer。"""

    def __init__(self) -> None:
        self.last_system = ""
        self.calls = 0

    async def review(self, *, pr, commits_text, diffs, static_findings_text=""):
        self.calls += 1
        self.last_system = static_findings_text
        from codereview_ai.domain.models import ReviewResult

        return ReviewResult(summary="ok")


# ── 归一化：ruff / semgrep ──────────────────────────────────────────────


async def test_analyze_normalizes_ruff_and_semgrep(tmp_path):
    runner = _FakeRunner()
    analyzer = StaticAnalyzer(runner=runner, workspace=tmp_path)
    diffs = [_py_diff("import os\n\ndef f():\n    return os\n"), _json("x\n")]
    findings = await analyzer.analyze(diffs)
    assert runner.calls == ["ruff", "semgrep"]  # 两者都跑

    ruff = [f for f in findings if f.source == "static:ruff"]
    assert len(ruff) == 1
    assert ruff[0].file == "a.py" and ruff[0].line == 3
    assert ruff[0].category == Category.BUG and ruff[0].side == "RIGHT"

    sg = [f for f in findings if f.source == "static:semgrep"]
    assert len(sg) == 1
    assert sg[0].line == 7 and sg[0].severity == Severity.HIGH
    assert "eval" in sg[0].content


#: 无 new_file_content 的 diff（删除文件）不应物化、不应触发工具
async def test_analyze_skips_files_without_content(tmp_path):
    runner = _FakeRunner()
    analyzer = StaticAnalyzer(runner=runner, workspace=tmp_path)
    deleted = FileDiff(
        old_path="a.py", new_path="/dev/null", diff="---", additions=0, deletions=3,
        change_type=ChangeType.DELETED_FILE, new_file_content="",
    )
    findings = await analyzer.analyze([deleted])
    assert findings == [] and runner.calls == []


async def test_analyze_disabled_returns_empty(tmp_path):
    analyzer = StaticAnalyzer(runner=_FakeRunner(), workspace=tmp_path, enabled=False)
    assert await analyzer.analyze([_py_diff("x")]) == []


async def test_analyze_degrades_when_tool_missing(tmp_path):
    runner = _FakeRunner()
    runner.raise_oserror = True
    analyzer = StaticAnalyzer(runner=runner, workspace=tmp_path)
    assert await analyzer.analyze([_py_diff("x")]) == []  # 缺工具降级为空，不抛


async def test_analyze_degrades_on_bad_json(tmp_path):
    class _BadRunner:
        async def run(self, tool, args, cwd) -> RunResult:
            return RunResult("not-json{{{", 0)

    analyzer = StaticAnalyzer(runner=_BadRunner(), workspace=tmp_path)
    assert await analyzer.analyze([_py_diff("x")]) == []


# ── prompt 注入 ─────────────────────────────────────────────────────────


def test_build_messages_injects_static_findings_text():
    from codereview_ai.domain.models import PullRequest

    diff = _py_diff("x\n")
    pr = PullRequest(
        provider="gitlab", repo_id="1", repo_full_name="o/r", web_url="u",
        pr_number=1, title="t", source_branch="s", target_branch="m",
        head_sha="a", base_sha="b",
    )
    msgs = build_messages(
        pr=pr, commits_text="c", diffs=[diff], skipped_files=[], cfg=ReviewerConfig(),
        static_findings_text="[high] [bug] a.py:3: 未使用导入",
    )
    system = msgs[0]["content"]
    assert "静态分析" in system and "不要重复报告" in system
    # 空文本 → 不加注入块
    plain = build_messages(
        pr=pr, commits_text="c", diffs=[diff], skipped_files=[], cfg=ReviewerConfig()
    )[0]["content"]
    assert "不要重复报告" not in plain


def test_render_static_findings_filters_by_files():
    from codereview_ai.domain.models import Finding

    f1 = Finding(content="A", category=Category.BUG, severity=Severity.HIGH,
                 existing_code="", file="a.py", line=3, source="static:ruff")
    f2 = Finding(content="B", category=Category.SECURITY, severity=Severity.MEDIUM,
                 existing_code="", file="b.js", line=5, source="static:semgrep")
    txt = render_static_findings([f1, f2], files={"a.py"})
    assert "a.py:3" in txt and "b.js" not in txt
    assert render_static_findings([f1], files={"nope.py"}) == ""


# ── 硬写入结果（review_in_groups 单组路径）────────────────────────────


async def test_review_in_groups_hardwrites_static_and_injects_prompt():
    from codereview_ai.domain.models import Finding, PullRequest

    diff = _py_diff("x\n")
    pr = PullRequest(
        provider="gitlab", repo_id="1", repo_full_name="o/r", web_url="u",
        pr_number=1, title="t", source_branch="s", target_branch="m",
        head_sha="a", base_sha="b",
    )
    static = [
        Finding(content="静态问题", category=Category.BUG, severity=Severity.HIGH,
                existing_code="", file="a.py", line=2, source="static:ruff"),
    ]
    reviewer = _EchoReviewer()
    result = await review_in_groups(reviewer, None, pr=pr, commits_text="c",
                                    diffs=[diff], static_findings=static)
    # prompt 注入：组内文件命中
    assert "a.py:2" in reviewer.last_system
    # 硬写入：静态 finding 进 result
    assert any(f.source == "static:ruff" for f in result.findings)
    assert len(result.findings) == 1

"""review/report_render 测试：分点清单的分组/编号/折叠/截断与 IM 动态正文拼装。

全程纯函数离线断言；这是 forge 总结评论、push 总结、IM 三渠道共用的渲染收口。
"""

from __future__ import annotations

from codereview_ai.domain.models import Category, Finding, Severity
from codereview_ai.review.report_render import (
    finding_title,
    render_findings_section,
    render_notification_body,
    render_summary_block,
)


def _f(
    sev: Severity = Severity.HIGH,
    content: str = "锁可能失效",
    title: str = "",
    file: str = "src/a.py",
    line: int | None = 12,
    suggestion: str | None = None,
) -> Finding:
    return Finding(
        content=content, category=Category.BUG, severity=sev,
        existing_code="", file=file, line=line, title=title, suggestion_code=suggestion,
    )


# ── finding_title ───────────────────────────────────────────────────────


def test_title_prefers_llm_title_and_clips():
    f = _f(title="锁" * 60)
    assert len(finding_title(f)) == 40 and finding_title(f).endswith("…")


def test_title_falls_back_to_content_when_empty():
    """agent 模式 code_comment 无 title → content 首行截断兜底。"""
    assert finding_title(_f(content="第一行\n第二行")) == "第一行 第二行"


# ── render_findings_section ─────────────────────────────────────────────


def test_section_groups_by_severity_with_numbered_details():
    findings = [
        _f(Severity.MEDIUM, content="中等问题"),
        _f(Severity.HIGH, content="高危一", line=3),
        _f(Severity.CRITICAL, content="严重问题"),
        _f(Severity.HIGH, content="高危二", line=8),
    ]
    md = render_findings_section(findings)
    assert "**发现的问题（4 条）**" in md
    # 组序按严重度：critical → high → medium
    assert md.index("🔴 严重（1）") < md.index("🟠 高危（2）") < md.index("🟡 中等（1）")
    # critical/high 展开编号详条，问题两段式
    assert "1. **[bug] 严重问题**（`src/a.py:12`）" in md
    assert "2. **[bug] 高危一**（`src/a.py:3`）" in md
    assert "- **问题**：高危一" in md
    # medium 单行要点，不参与编号；无 title 时 content 直接作粗体正文（不重复）
    assert "- 🟡 中等 **[bug] 中等问题**（`src/a.py:12`）" in md


def test_section_renders_suggestion_code_only_when_allowed():
    code = "with lock:\n    pass"
    with_code = render_findings_section([_f(suggestion=code)])
    assert "```python" in with_code and "with lock:" in with_code
    without = render_findings_section([_f(suggestion=code)], with_code=False)
    assert "```" not in without


def test_section_folds_and_clips_for_tight_budget():
    findings = [_f(Severity.MEDIUM, content="中" * 300), _f(Severity.LOW, content="小问题")]
    md = render_findings_section(
        findings, fold=("medium", "low"), max_item_chars=150,
    )
    assert "🟡 中等 ×1（详见完整报告）" in md
    assert "🟢 轻微 ×1（详见完整报告）" in md
    assert "中" * 300 not in md  # 折叠组整组不展开


def test_section_clips_long_content():
    md = render_findings_section([_f(content="长" * 200)], max_item_chars=50)
    assert ("长" * 49 + "…") in md and ("长" * 200) not in md


def test_section_empty_findings_returns_empty():
    assert render_findings_section([]) == ""


def test_section_handles_unknown_severity_and_unanchored_line():
    """未知严重度不丢、原样成组；line=None 显示纯文件路径。"""
    f = _f(content="怪条目", line=None)
    f.severity = "blocker"  # 直塞野值模拟脏数据
    md = render_findings_section([f])
    assert "blocker（1）" in md and "（`src/a.py`）" in md


# ── render_summary_block / render_notification_body ────────────────────


def test_summary_block_empty_when_blank():
    assert render_summary_block("  ") == ""


def test_notification_body_lists_findings_then_summary():
    body = render_notification_body(
        findings=[_f()], summary_md="改动主要是加锁，建议先修再合入。")
    assert "**发现的问题（1 条）**" in body
    assert "1. **[bug]" in body
    assert body.rstrip().endswith("建议先修再合入。")
    assert "**总结**" in body


def test_notification_body_falls_back_to_summary_md_without_findings():
    """日报等无 findings 路径：summary_md 整体透传（表格原样保留）。"""
    table = "| 状态 | 数量 |\n|---|---|\n| 完成 | 2 |"
    body = render_notification_body(findings=[], summary_md=table)
    assert body == table


def test_notification_body_summary_md_unused_when_summary_present():
    """review 类：summary（通俗总结）与 findings 同时给时，不再透传原始 summary_md。"""
    body = render_notification_body(findings=[_f()], summary="大白话总结")
    assert "大白话总结" in body and "**总结**" in body

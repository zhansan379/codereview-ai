"""审查成果的统一「分点」渲染：forge 总结评论 / push 轨总结 / IM 三渠道共用。

观感契约（对齐用户拍板的样张）：
- 按严重度分组分点——🔴 严重 / 🟠 高危 展开成编号详条（问题/建议两段式，平台端带
  修复代码块）；🟡 中等 / 🟢 轻微 压成单行要点；预算紧的渠道（企微 4096B）可把
  后两组整组折叠成计数行。
- `render_summary_block` 给通俗总结加 **总结** 小节头，跟逐条清单区分开。
- 条目正文（content）里的换行压成空格：markdown 列表项内的裸换行会打断分点结构。

纯函数、与 LLM 无关：输入是已成形的 Finding 结构化数据，展示格式全部在这里收口。
"""

from __future__ import annotations

from codereview_ai.domain.models import Finding

#: 严重度展示顺序与徽标（未知严重度追加在末尾，不丢条目）。
_SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")
_SEVERITY_BADGE: dict[str, str] = {
    "critical": "🔴 严重", "high": "🟠 高危", "medium": "🟡 中等", "low": "🟢 轻微",
}
#: 展开为编号详条目（问题/建议两段式）的严重度；其余压成单行要点。
_DETAILED_SEVERITIES: tuple[str, ...] = ("critical", "high")
_MAX_TITLE_CHARS = 40


def _oneline(text: str) -> str:
    """换行与连续空白压成单空格，保证列表项不被内部换行打散。"""
    return " ".join((text or "").split())


def _clip(text: str, limit: int | None) -> str:
    """按字符数截断（None 不截）；多字节安全由调用方的字节级截断兜底。"""
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def finding_title(f: Finding) -> str:
    """条目标题：LLM 给的 title 优先（agent 模式没有），缺省取 content 首段截断。"""
    t = (f.title or "").strip()
    if t:
        return _clip(_oneline(t), _MAX_TITLE_CHARS)
    return _clip(_oneline(f.content), _MAX_TITLE_CHARS)


def _location(f: Finding) -> str:
    """条目定位串：锚到行给 `file:line`，未锚到给 `file`。"""
    return f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"


def _group_by_severity(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    """按严重度聚组（组内保持原顺序）；未知严重度排在已知组之后，不丢。"""
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(str(f.severity), []).append(f)
    order = [s for s in _SEVERITY_ORDER if s in groups]
    order += [s for s in groups if s not in _SEVERITY_ORDER]
    return [(s, groups[s]) for s in order]


def render_findings_section(
    findings: list[Finding],
    *,
    with_code: bool = True,
    fold: tuple[str, ...] = (),
    max_item_chars: int | None = None,
) -> str:
    """把 findings 渲染成分点问题清单；空列表返回空串（调用方据此省略区块）。

    - `with_code`：条目带 suggestion_code 时是否渲染修复代码块（forge 支持 fence，
      IM 渠道 markdown 子集不稳，一律 False 只发文字）。
    - `fold`：整组折叠成计数行的严重度（企微 4096B 预算下 medium/low 折叠）。
    - `max_item_chars`：每条正文的字符级截断（IM 渠道用；None 不截）。
    """
    if not findings:
        return ""
    lines = [f"**发现的问题（{len(findings)} 条）**"]
    num = 0
    for sev, items in _group_by_severity(findings):
        badge = _SEVERITY_BADGE.get(sev, sev)
        if sev in fold:
            lines += ["", f"- {badge} ×{len(items)}（详见完整报告）"]
            continue
        lines += ["", f"**{badge}（{len(items)}）**", ""]
        if sev in _DETAILED_SEVERITIES:
            for f in items:
                num += 1
                lines.append(
                    f"{num}. **[{str(f.category)}] {finding_title(f)}**（{_location(f)}）"
                )
                lines.append(f"   - **问题**：{_clip(_oneline(f.content), max_item_chars)}")
                if with_code and (f.suggestion_code or "").strip():
                    lines += [
                        "   - **建议修复**：",
                        "```python",
                        (f.suggestion_code or "").rstrip(),
                        "```",
                    ]
        else:
            for f in items:
                detail = _clip(_oneline(f.content), max_item_chars)
                if not (f.title or "").strip():
                    # 无 title（agent 模式）：title 本就是 content 截断，粗体直接承载
                    # 截断后的正文，避免同一句话在条目里出现两遍。
                    lines.append(
                        f"- {badge} **[{str(f.category)}] {detail}**（{_location(f)}）"
                    )
                else:
                    lines.append(
                        f"- {badge} **[{str(f.category)}] {finding_title(f)}**（{_location(f)}）："
                        f"{detail}"
                    )
    return "\n".join(lines)


def render_summary_block(summary: str, *, label: str = "总结") -> str:
    """通俗总结区块：小节头 + 原文段落；空总结返回空串。"""
    s = (summary or "").strip()
    if not s:
        return ""
    return f"**{label}**\n\n{s}"


def branch_meta(source: str, target: str) -> str:
    """PR/MR 分支信息行：`源分支 f → 目标分支 m`；缺任一端或两端相同返回空串。

    两端相同（push 轨套壳的 PR 源=目标=分支名）不算真正的分支对，省略防空穿帮。
    """
    s, t = (source or "").strip(), (target or "").strip()
    if s and t and s != t:
        return f"源分支 {s} → 目标分支 {t}"
    return ""


def render_notification_body(
    *,
    findings: list[Finding],
    summary: str = "",
    summary_md: str = "",
    fold: tuple[str, ...] = (),
    max_item_chars: int | None = None,
) -> str:
    """IM 渠道共用的动态正文：分点清单 + 总结；无 findings 时退回 `summary_md` 原文。

    总结来源优先级：显式 `summary` > 有 findings 时的 `summary_md`（review 类通知里
    它就是 LLM 通俗总结，加 **总结** 小节头）> 无 findings 时 `summary_md` 整体透传
    不加头（日报等 `send_markdown` 路径，正文是表格整篇）。渠道字节预算的截断由
    各 sink 自行收口（这里只做字符级条目截断）。
    """
    parts: list[str] = []
    if findings:
        parts.append(render_findings_section(
            findings, with_code=False, fold=fold, max_item_chars=max_item_chars,
        ))
    block = render_summary_block(summary or (summary_md if findings else ""))
    if block:
        parts.append(block)
    elif summary_md.strip():
        parts.append(summary_md)
    return "\n\n".join(p for p in parts if p)

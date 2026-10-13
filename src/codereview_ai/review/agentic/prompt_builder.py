"""OCR 主任务 user 消息拼装 + 组内共享的 diff 渲染（照搬 main_task_user.md）。

- `build_main_task_intro`：把本组 diffs / 其它变更文件 / 审查清单 / plan / 已确认发现
  填进 `MAIN_TASK_USER`。plan 或 confirmed 为空时，把对应整段剥掉（OCR `stripEmptyPlanBlock`），
  不残留空占位符。
- `system_rule` 默认用 OCR `rule_docs/default.md`（正确性/安全/性能/可维护性/测试覆盖）。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime

from codereview_ai.domain.models import FileDiff
from codereview_ai.review.agentic.prompts import DEFAULT_REVIEW_CHECKLIST, MAIN_TASK_USER


def render_diffs(diffs: Sequence[FileDiff]) -> str:
    """拼接本组各文件的 unified diff 文本（喂给 plan/main 的 `{{diffs}}`）。"""
    texts = [d.diff for d in diffs if d.diff and d.diff.strip()]
    return "\n".join(texts) if texts else "(no diff content)"


def current_time_line() -> str:
    """`{{current_system_date_time}}`：人类可读的 UTC 时刻（OCR 亦注入当前时间）。

    `CR_BENCH_FREEZE_TIME` 有值时返回该固定时刻：基准需要可复现。时间注入让每次运行的
    prompt 都不同（精度到分钟），实测同配置两次跑出的评论**没有一条正文相同**、位置重合
    只有 35~51%，任何「改动前后」对比都会被这层噪声淹没。生产不设此变量，行为不变。
    """
    frozen = (os.environ.get("CR_BENCH_FREEZE_TIME") or "").strip()
    if frozen:
        return frozen
    return datetime.now(UTC).isoformat(timespec="minutes").replace("+00:00", "")


def _strip_empty_block(text: str, heading: str, placeholder: str) -> str:
    """若 placeholder 值为空，把「heading 行 + placeholder 行」整段从模板剥掉。"""
    line = f"{heading}\n{placeholder}\n"
    return text.replace(line, "")


def _fill(value: str | None, default: str = "(none)") -> str:
    return (value.strip() if value and value.strip() else default)


def build_main_task_intro(
    *,
    diffs: Sequence[FileDiff],
    change_files: Sequence[str] = (),
    plan: str = "",
    checklist: str = DEFAULT_REVIEW_CHECKLIST,
    confirmed: str = "",
    requirement_background: str = "",
) -> str:
    """拼 OCR 主任务 user 消息；返回可直接当会话首条 user 的字符串。"""
    content = MAIN_TASK_USER
    content = content.replace("{{change_files}}", "\n".join(change_files) or "(none)")
    content = content.replace("{{diffs}}", render_diffs(diffs))
    content = content.replace("{{current_system_date_time}}", current_time_line())
    content = content.replace("{{requirement_background}}",
                              _fill(requirement_background))
    content = content.replace("{{system_rule}}", checklist.strip())

    if not plan.strip():
        content = _strip_empty_block(content, "### Review Plan", "{{plan_guidance}}")
    else:
        content = content.replace("{{plan_guidance}}", plan.strip())

    if not confirmed.strip():
        content = _strip_empty_block(content, "### Previously Confirmed Findings",
                                     "{{confirmed_comments}}")
    else:
        content = content.replace("{{confirmed_comments}}", confirmed.strip())

    return content

"""OCR plan_task 阶段（照搬 plan_task_*.md + PlanRequired 门控）。

- `plan_required`：复刻 OCR `PlanRequired` 门控——单个超大文件（churn≥line_threshold）或
  组合 churn≥group_line_threshold（需 ≥2 文件）才跑 plan；小改动整包单组不值得规划。
- `build_plan_messages`：把 diffs/其它变更/审查清单/可用工具描述填进 `PLAN_TASK_USER`。
- `run_plan_phase`：一次无工具 chat → 纯文本结构化计划（`Summary:` + `Issues` …）；
  失败返回 ""（略过 plan，OCR 亦然，不降级主链）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from codereview_ai.domain.models import FileDiff
from codereview_ai.review.agentic.llmloop import AgentLLM
from codereview_ai.review.agentic.prompt_builder import current_time_line, render_diffs
from codereview_ai.review.agentic.prompts import (
    DEFAULT_REVIEW_CHECKLIST,
    PLAN_TASK_SYSTEM,
    PLAN_TASK_USER,
)

logger = logging.getLogger("codereview_ai.agentic.planner")

#: plan 门控阈值（OCR template.go PlanRequired 的口径）：单个大文件 / 组 combined churn。
PLAN_SINGLE_LINE_THRESHOLD = 300
PLAN_GROUP_LINE_THRESHOLD = 600

#: 计划阶段"可用上下文工具"的描述（`{{plan_tools}}`）。plan 不真正给工具，只让模型知道
#: 它能计划未来用哪些工具查证（OCR 只列 PlanToolDefs，不注入函数）。对齐六只读工具。
DEFAULT_PLAN_TOOLS = (
    "- read_file(file_path, start_line, end_line): read a file range in the repo\n"
    "- grep_repo(search_text[, case_sensitive][, file_patterns]): search text across the repo\n"
    "- file_find(query_name): find file paths by name\n"
    "- file_read_diff(path_array): return parsed diffs by path\n"
    "- code_comment(comments): report a review comment (the only output channel)\n"
    "- task_done(state): declare review finished (DONE|FAILED)"
)


def plan_required(
    diffs: Sequence[FileDiff],
    *,
    line_threshold: int = PLAN_SINGLE_LINE_THRESHOLD,
    group_line_threshold: int = PLAN_GROUP_LINE_THRESHOLD,
) -> bool:
    """复刻 OCR PlanRequired：单大文件或组 churn 超阈值才需要 plan。"""
    if not diffs:
        return False
    if len(diffs) == 1:
        return (diffs[0].additions + diffs[0].deletions) >= line_threshold
    total = sum(d.additions + d.deletions for d in diffs)
    return total >= group_line_threshold


def build_plan_messages(
    *,
    diffs: Sequence[FileDiff],
    change_files: Sequence[str] = (),
    checklist: str = DEFAULT_REVIEW_CHECKLIST,
    plan_tools: str = DEFAULT_PLAN_TOOLS,
) -> list[dict[str, str]]:
    """拼 OCR plan 阶段 system+user 消息。"""
    system = PLAN_TASK_SYSTEM.replace("{{plan_tools}}", plan_tools)
    user = PLAN_TASK_USER
    user = user.replace("{{change_files}}", "\n".join(change_files) or "(none)")
    user = user.replace("{{diffs}}", render_diffs(diffs))
    user = user.replace("{{current_system_date_time}}", current_time_line())
    user = user.replace("{{requirement_background}}", "(none)")
    user = user.replace("{{system_rule}}", checklist.strip())
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def run_plan_phase(llm: AgentLLM, messages: list[dict[str, str]]) -> str:
    """一次无工具 chat 产出计划文本；失败返回 ""（不降级，只略过 plan）。"""
    try:
        turn = await llm.chat(messages, [])
    except Exception as exc:  # noqa: BLE001 —— plan 失败不影响主链，OCR 亦容忍
        logger.warning("agentic plan 阶段失败，略过 plan：%s", exc)
        return ""
    text = str(getattr(turn, "content", None) or "").strip()
    if not text:
        logger.info("agentic plan 阶段返回空，略过 plan")
        return ""
    return text

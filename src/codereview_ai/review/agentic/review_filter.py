"""OCR review_filter 阶段（照搬 review_filter_*.md + filterTools 契约）。

- 事实核查：只删 diff **铁证**体现为错（Ground A/B）的评论，存疑一律批过。
- 工具契约（OCR filterTools）：模型必须恰好调一个——
  - `report_incorrect_comments({analysis, comment_ids})` —— 仅当能点名反驳 diff 行；
  - `approve_all_comments` —— 其余一切情况（默认）。
- 返回要删除的候选索引集合（"c-N" → 候选切片索引，映射回原列表索引）。
- **per-round 隔离**：只把"本轮新增"的评论作候选（OCR from baseline），历史评论不重审。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from codereview_ai.review.agentic.llmloop import AgentLLM, AgentTurn
from codereview_ai.review.agentic.prompts import (
    REVIEW_FILTER_TASK_SYSTEM,
    REVIEW_FILTER_TASK_USER,
)

logger = logging.getLogger("codereview_ai.agentic.filter")


def filter_tools() -> list[dict[str, Any]]:
    """review_filter 的两个裁决工具（OpenAI 风格 JSON-schema）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "report_incorrect_comments",
                "description": "Report the ids of comments this diff proves factually wrong.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "analysis": {"type": "string",
                                     "description": "For each dropped id, name the diff line "
                                                    "that establishes Ground A or Ground B."},
                        "comment_ids": {"type": "array",
                                        "items": {"type": "string"},
                                        "description": "ids like \"c-0\". Empty array = none."},
                    },
                    "required": ["comment_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "approve_all_comments",
                "description": "Approve all comments, keeping the entire candidate set.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]


def build_filter_messages(
    *,
    comments: list[dict[str, Any]],
    group_diff_text: str,
) -> list[dict[str, str]]:
    """拼 review_filter 的 system+user（`{{diff}}` 组 diff、`{{comments}}` JSON）。"""
    user = REVIEW_FILTER_TASK_USER
    user = user.replace("{{diff}}", group_diff_text or "(no diff content)")
    user = user.replace("{{comments}}",
                        json.dumps(comments, ensure_ascii=False, indent=1))
    return [
        {"role": "system", "content": REVIEW_FILTER_TASK_SYSTEM},
        {"role": "user", "content": user},
    ]


def comments_json(findings: list[Any]) -> list[dict[str, Any]]:
    """把候选 findings 序列化成带 `c-N` id 的 JSON（OCR buildGroupFilterCommentsJSON）。"""
    out: list[dict[str, Any]] = []
    for i, f in enumerate(findings):
        out.append({
            "id": f"c-{i}",
            "path": f.file,
            "line": f.line,
            "category": f.category.value,
            "severity": f.severity.value,
            "suggestion": f.content,
        })
    return out


def _ids_to_indices(candidate_count: int, comment_ids: list[Any]) -> set[int]:
    """把 report_incorrect_comments 的 "c-N" 映射回候选列表索引；越界/坏值忽略。"""
    idx: set[int] = set()
    if not isinstance(comment_ids, list):
        return idx
    for cid in comment_ids:
        if isinstance(cid, str) and cid.startswith("c-"):
            n = cid[2:]
            if n.isdigit() and 0 <= int(n) < candidate_count:
                idx.add(int(n))
    return idx


def parse_filter_result(turn: AgentTurn, candidate_count: int) -> set[int]:
    """解析裁决：report_incorrect_comments → 删对应索引；approve_all/无调用/异常 → 空。"""
    for tc in turn.tool_calls:
        if tc.name == "report_incorrect_comments":
            args = tc.args or {}
            return _ids_to_indices(candidate_count, args.get("comment_ids"))
        if tc.name == "approve_all_comments":
            return set()
    # 既无调用也无内容 → 默认批过（最安全：宁可保留）
    logger.info("review_filter 未产出裁决工具调用，默认全部批过")
    return set()


async def run_review_filter(
    llm: AgentLLM,
    findings: list[Any],
    *,
    group_diff_text: str,
) -> list[Any]:
    """对候选 findings 跑一次事实核查，返回**保留**的列表（改成压缩 copy 无副作用）。"""
    if not findings:
        return findings
    messages = build_filter_messages(comments=comments_json(findings),
                                     group_diff_text=group_diff_text)
    try:
        turn = await llm.chat(messages, filter_tools())
    except Exception as exc:  # noqa: BLE001 —— 过滤失败不降级，原样保留
        logger.warning("review_filter 阶段失败，跳过过滤：%s", exc)
        return findings
    drop = parse_filter_result(turn, len(findings))
    if not drop:
        return findings
    kept = [f for i, f in enumerate(findings) if i not in drop]
    logger.info("review_filter 丢弃 %d 条（共 %d 条候选）", len(drop), len(findings))
    return kept

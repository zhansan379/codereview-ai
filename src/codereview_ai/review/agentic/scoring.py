"""评分收尾阶段（OCR 没有，属我们自创的 0-100 卡片救回）。

对已产出的全部 findings 送一次无工具 chat，让模型按 5 维度打分并保证总分=100：
correctness(40)/security(30)/practices(20)/performance(5)/commit_quality(5)。
- `build_score_messages`：把 findings（content/category/severity/file）+ diff 摘要文本拼进
  `SCORING_TASK_USER`（`{{comments}}` 注入，`{{diff}}` 注入）→ system+user。
- `run_scoring`：一次 `llm.chat(msgs, [])` → `json.loads(content)` 解析 `ReviewScores`；
  越界/坏 JSON/失败归 0 并打 warning，**不降级**（findings 已到手，分数只是卡片）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from codereview_ai.domain.models import ReviewScores
from codereview_ai.review.agentic.llmloop import AgentLLM
from codereview_ai.review.agentic.prompts import (
    SCORING_TASK_SYSTEM,
    SCORING_TASK_USER,
)

logger = logging.getLogger("codereview_ai.agentic.scoring")

#: 5 维度合法值域（OCR 无，评审维度沿用既有卡片口径）；越界即弃该维、按 0 计。
_HINTS: dict[str, tuple[str, int]] = {
    "correctness": ("correctness 0-40", 40),
    "security": ("security 0-30", 30),
    "practices": ("practices 0-20", 20),
    "performance": ("performance 0-5", 5),
    "commit_quality": ("commit_quality 0-5", 5),
}


def _finding_briefs(findings: list[Any]) -> str:
    """把 findings 压成紧凑的逐条简报喂给评分模型（避免喂整份 context 重复烧 token）。"""
    if not findings:
        return "(no findings)"
    lines = []
    for f in findings:
        lines.append(
            f"- [{getattr(f, 'category', '')}] {getattr(f, 'file', '')}"
            f"@{getattr(f, 'line', '-')} ({getattr(f, 'severity', '')}): "
            f"{getattr(f, 'content', '')}"
        )
    return "\n".join(lines)


def build_score_messages(
    *,
    findings: list[Any],
    group_diff_text: str = "",
) -> list[dict[str, str]]:
    """拼评分阶段的 system+user（`{{comments}}` findings 简报、`{{diff}}` diff 摘要）。"""
    user = SCORING_TASK_USER
    user = user.replace("{{comments}}", _finding_briefs(findings) or "(none)")
    user = user.replace("{{diff}}", group_diff_text or "(no diff content)")
    return [
        {"role": "system", "content": SCORING_TASK_SYSTEM},
        {"role": "user", "content": user},
    ]


def _clamp(value: Any, max_val: int) -> int:
    """容忍字符串/浮点/负数/越界——取整后钳到 [0, max]；坏值返回 0。"""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, max_val))


def _parse_scores(content: str) -> ReviewScores | None:
    """从模型返回文本里提取 JSON 并解析为 ReviewScores；失败返回 None。"""
    text = (content or "").strip()
    if not text:
        return None
    # 宽松提取：先整体 json.loads，失败再找最外层 {...} 块（模型常裹 markdown fence）。
    payload: Any = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                payload = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None
    if not isinstance(payload, dict):
        return None
    return ReviewScores(
        correctness=_clamp(payload.get("correctness"), _HINTS["correctness"][1]),
        security=_clamp(payload.get("security"), _HINTS["security"][1]),
        practices=_clamp(payload.get("practices"), _HINTS["practices"][1]),
        performance=_clamp(payload.get("performance"), _HINTS["performance"][1]),
        commit_quality=_clamp(payload.get("commit_quality"), _HINTS["commit_quality"][1]),
    )


async def run_scoring(llm: AgentLLM, messages: list[dict[str, str]]) -> ReviewScores:
    """一次无工具 chat 产出 0-100 评分；失败/坏 JSON 归 0 并打 warning（不降级）。"""
    try:
        turn = await llm.chat(messages, [])
    except Exception as exc:  # noqa: BLE001 —— 评分失败不影响 findings
        logger.warning("agentic 评分阶段失败，分数归 0：%s", exc)
        return ReviewScores()
    scores = _parse_scores(str(getattr(turn, "content", None) or ""))
    if scores is None:
        logger.warning("agentic 评分阶段未解析出评分 JSON，分数归 0")
        return ReviewScores()
    logger.info("agentic 评分: correctness=%d security=%d practices=%d "
                "performance=%d commit_quality=%d",
                scores.correctness, scores.security, scores.practices,
                scores.performance, scores.commit_quality)
    return scores

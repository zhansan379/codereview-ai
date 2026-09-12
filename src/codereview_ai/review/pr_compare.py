"""PR 级聚合：把同一 PR 的多轮已完成审查串成收敛时间线 + 末轮四桶。

纯函数、不碰 DB/ORM(与 `compare.py` 同范式):调用方(API)负责取数并按
`(provider, repo_id, pr_number)` 分组,本模块只做相邻轮差量计算:

- 对每个 PR,遍历已完成任务(需按 `task.id` 升序)：
  - 首轮 → `bucket_compare([], findings_0, covered_0)`，全部进 `new`。
  - 第 i 轮 → 对比上一轮 `bucket_compare(findings_{i-1}, findings_i, covered_i)`。
- 每轮只留四桶**计数**（时间线 `+new / -resolved` 用）；
  **末轮额外带完整四桶 dict**（`last_delta`），供页面展示明细。

覆盖集语义沿用 compare：不在覆盖集的 before-finding 进 `not_reviewed`（保守，
不算已修）。`_FindRow` 适配在调用方完成，本模块直接用已适配的行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codereview_ai.review.compare import CompareResult, bucket_compare

#: 与 `compare.py` 的 Bucket 同构：finding 的中平铺 dict。
Bucket = list[dict[str, Any]]


@dataclass
class PrRound:
    """一轮已完成审查的收敛情况（只含计数，供时间线渲染）。"""

    id: int
    head_sha: str
    delta: dict[str, int] = field(default_factory=dict)


@dataclass
class PrDelta:
    """一个 PR 的聚合结果。"""

    key: str
    rounds: list[PrRound]
    last_delta: dict[str, Bucket] = field(default_factory=dict)
    #: 末轮“解决了多少本就该收敛的”：resolved/(resolved+persisting+not_reviewed)。
    rate_pct: int = 0


def _delta_counts(result: CompareResult) -> dict[str, int]:
    return {
        "new": len(result.new),
        "persisting": len(result.persisting),
        "resolved": len(result.resolved),
        "not_reviewed": len(result.not_reviewed),
    }


def group_pr_deltas(
    pr_task_groups: dict[tuple[str, str, int], list[tuple[int, str, list[Any], set[str]]]],
) -> list[PrDelta]:
    """把同一 PR 的各轮（task_id, head_sha, findings, covered）转成收敛时间线。

    - `pr_task_groups[k]` 需按 task_id 升序，且只含已完成的 MR 任务。
    - `findings` 为已做 `_FindRow` 适配的行，`covered` 为覆盖集路径集合。
    - 返回按每个 PR 最近一轮排在前面（调用方可再按最近活动总排序）。
    """
    out: list[PrDelta] = []
    for key, rows in pr_task_groups.items():
        rounds: list[PrRound] = []
        prev_findings: list[Any] = []
        last_delta: dict[str, Bucket] = {}
        for task_id, head_sha, findings, covered in rows:  # 已升序
            result = bucket_compare(prev_findings, findings, covered)
            rounds.append(PrRound(id=task_id, head_sha=head_sha, delta=_delta_counts(result)))
            prev_findings = findings
            last_delta = {
                "new": result.new,
                "persisting": result.persisting,
                "resolved": result.resolved,
                "not_reviewed": result.not_reviewed,
            }

        # 收敛率：只看末轮“本该收敛的”里实际解决了多少。
        lr = last_delta
        counted = len(lr["resolved"]) + len(lr["persisting"]) + len(lr["not_reviewed"])
        rate = round(len(lr["resolved"]) / counted * 100) if counted else 100
        out.append(PrDelta(
            key=":".join(str(x) for x in key),
            rounds=rounds,
            last_delta=last_delta,
            rate_pct=rate,
        ))
    return out

"""预算闸门（DESIGN §12.4）：前置成本预估 + 超限停止调度剩余组。

复刻 OCR `estimate.go` 的量级预估：按 diff 与文件内容字符数折算 token，只做**调度前**
量级判断，真实用量事后按 API usage 上报。`projected = used + Σ estimate(group)` 超
`MaxTokensBudget` 就不再调度剩余组（在飞组可跑完），返回部分评论而非丢弃全部。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codereview_ai.domain.models import FileDiff

#: 每字符约合 token（英文≈1 token/4字符，代码偏密取 4）。
_CHAR_PER_TOKEN = 4

#: 默认整次审查 token 预算上限（agent 探秘 + 评论）。
MaxTokensBudget = 80_000


def estimate_tokens(diffs: list[FileDiff]) -> int:
    """近似预估一组 diffs 的 agent 会话 token 开销（量级即可，§12.4 只做前置门限）。"""
    chars = sum(len(d.diff) + len(d.new_file_content) for d in diffs)
    return max(1, chars // _CHAR_PER_TOKEN)


@dataclass
class BudgetGate:
    """按前置预估调度文件组；超预算即停止调度剩余组，不丢弃已完成的。

    - `schedule(group_estimate)`：占用预算。
    - `projected(extra: list[int])`：当前已用 + 剩余候选组预估的合计，用于调度决策。
    """

    max_budget: int = MaxTokensBudget
    used: int = 0
    stopped: bool = False
    skipped_groups: int = field(default=0)

    def projected(self, extra: list[int]) -> int:
        return self.used + sum(extra)

    def can_schedule(self, group_estimate: int) -> bool:
        return not self.stopped and self.projected([group_estimate]) <= self.max_budget

    def schedule(self, group_estimate: int) -> None:
        self.used += group_estimate

    def stop(self, remaining_estimates: list[int]) -> None:
        """超预算：停止调度，记录未飞组数。"""
        self.stopped = True
        self.skipped_groups += len(remaining_estimates)

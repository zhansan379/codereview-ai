"""前后两次审查的 findings 增量对比（对齐 OCR `session/compare.go` 的四桶语义）。

- `CompareResult`：`new` / `persisting` / `resolved` / `not_reviewed` 四桶。
- `bucket_compare(before, after, after_covered)`：按 `finding_fingerprint`
  （file + content.lower()）做 **multiset 差**，纯函数、可离线单测：
  - 同一指纹 before/after 都出现 → `persisting`(N,M 取最小值,带 after 副本/当前行号)；
  - after 多出的 → `new`；before 多出且文件被 after 覆盖 → `resolved`；
  - before 多出但文件 **不在** after 覆盖集 → `not_reviewed`（after 这次没碰，不算已修）。

不读也不改 `review_finding.status`（那是 `reconcile_findings` 原地状态机改过的历史态），
本模块只做**纯指纹快照差**，避开历史错乱。DB 读取在调用方（review_repo / API）完成。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from codereview_ai.review.increments import finding_fingerprint

#: 桶内元素：finding 的中平铺 dict（content/category/severity/file/line/old_line 等）。
Bucket = list[dict[str, Any]]


@dataclass
class CompareResult:
    new: Bucket = field(default_factory=list)
    persisting: Bucket = field(default_factory=list)
    resolved: Bucket = field(default_factory=list)
    not_reviewed: Bucket = field(default_factory=list)


def _flat(f: Any) -> dict[str, Any]:
    """把 Finding 平铺成展示字典（含当前行号；无属性时兜底空串）。"""
    return {
        "content": getattr(f, "content", ""),
        "category": str(getattr(f, "category", "")),
        "severity": str(getattr(f, "severity", "")),
        "file": getattr(f, "file", "") or "",
        "line": getattr(f, "line", None),
        "old_line": getattr(f, "old_line", None),
        "existing_code": getattr(f, "existing_code", "") or "",
        "source": getattr(f, "source", "") or "",
    }


def bucket_compare(
    before: list[Any],
    after: list[Any],
    after_covered: set[str],
) -> CompareResult:
    """按指纹 multiset 差把两轮 finding 分成四桶（OCR `Compare` 语义，纯函数）。"""
    after_fps = Counter(finding_fingerprint(f) for f in after)
    before_fps = Counter(finding_fingerprint(f) for f in before)

    result = CompareResult()
    # after 侧：前 before_fps 个同指纹副本算 persisting（带 after 快照），多出的算 new。
    placed_persist: Counter[str] = Counter()
    for f in after:
        fp = finding_fingerprint(f)
        if placed_persist[fp] < before_fps[fp]:
            placed_persist[fp] += 1
            result.persisting.append(_flat(f))
        else:
            result.new.append(_flat(f))

    # before 侧：多出的副本，文件被 after 覆盖 → resolved，否则 not_reviewed。
    placed_before: Counter[str] = Counter()
    for f in before:
        fp = finding_fingerprint(f)
        if placed_before[fp] < after_fps[fp]:
            placed_before[fp] += 1
            continue  # 已被 after 配对（persisting）
        if f.file in after_covered:
            result.resolved.append(_flat(f))
        else:
            result.not_reviewed.append(_flat(f))
    return result

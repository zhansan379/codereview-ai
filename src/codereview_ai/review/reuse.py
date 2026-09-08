"""跨会话「未变更文件」复用（对齐 OCR `review_item_reused` 的核心价值）。

- `file_content_key` / `covered_file_map`：把一轮 diffs 归一成 `{new_path: sha1(new)}`，
  存 `review_task.diff_snapshot`（dead 列复用，DESIGN §5）。
- `prune_unchanged`：增量轮里，`new_path` 内容哈希未变的文件不再喂 agent，直接复用上次
  「已审过干净」的结果（省 token/时）。只做 agent 输入的**路径过滤**，不改 finding 级
  `dedup_findings` 去重与 `reconcile_findings` 状态机。

纯函数、可离线单测；DB 存取在 `review_repo`。
"""

from __future__ import annotations

import hashlib

from codereview_ai.domain.models import FileDiff


def file_content_key(d: FileDiff) -> str | None:
    """新侧内容哈希；无新侧内容（删除/纯旧侧）返回 None（视为「必须保留」）。"""
    content = d.new_file_content
    if not content:
        return None
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def covered_file_map(diffs: list[FileDiff]) -> dict[str, str]:
    """`{new_path 归一: sha1(new)}`，仅收有新侧内容的文件。"""
    out: dict[str, str] = {}
    for d in diffs:
        key = d.new_path.removeprefix("/")
        h = file_content_key(d)
        if h is not None:
            out[key] = h
    return out


def prune_unchanged(diffs: list[FileDiff], last_covered: dict[str, str] | None) -> list[FileDiff]:
    """按上次覆盖集剪掉内容未变的文件；无覆盖集/无新侧内容 → 原样保留。"""
    if not last_covered:
        return list(diffs)
    kept: list[FileDiff] = []
    for d in diffs:
        key = d.new_path.removeprefix("/")
        h = file_content_key(d)
        if h is None or last_covered.get(key) != h:
            kept.append(d)
    return kept

"""文件名组编排：per-file / 整包 / LLM 语义分组 + 降级（DESIGN §7.2，1:1 抄 OCR grouping.go）。

分组只喂**文件元数据**（`STATUS path (+N/-M)`，不含 diff 内容），便宜且聚焦。
决策链（OCR grouping.go）：
  1. `len ≤ 1` → per-file 单组（每个文件一组）。
  2. `files < 4 且 churn < 200` → **整包单组**（小改动不值得分组）。
  3. `files ≥ 4` → 调 LLM 语义分组，把 message_en/zh.properties 这类关联文件并进一组。
  4. LLM 失败 → 降级 per-file。
强约束：`max_files_per_group = 10` 切超大组；每组 churn 超预算 → 拆成单文件组。

LLM 分组是可注入接口（`GroupLLM`），离线测试用 fake；网络只出现在该接口实现里。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from codereview_ai.domain.models import ChangeType, FileDiff

logger = logging.getLogger("codereview_ai.grouping")

#: 切超大组的硬上限（OCR maxFilesPerGroup=10）
MAX_FILES_PER_GROUP = 10
#: 小改动阈值：文件 < 4 且总 churn < 200 → 整包单组（OCR BundleAll 短路）
SMALL_FILE_THRESHOLD = 4
SMALL_CHURN_THRESHOLD = 200
#: 每组 churn 预算（行），按单组 token 预算兜底折算（OCR 80% MaxTokens）
GROUP_CHURN_BUDGET = 1000

#: ChangeType → git 状态字母（formatDiffEntry 的 STATUS）
_CHANGE_STATUS = {
    ChangeType.NEW_FILE: "A",
    ChangeType.DELETED_FILE: "D",
    ChangeType.RENAMED_FILE: "R",
    ChangeType.MODIFIED: "M",
}


def _status(d: FileDiff) -> str:
    """文件变更状态字母（A/D/R/M）。"""
    return _CHANGE_STATUS.get(d.change_type, "M")


def format_diff_entry(d: FileDiff) -> str:
    """渲染分组入参的单行元数据：`STATUS  path (+N/-M)`（不含 diff 内容）。"""
    path = d.new_path or d.old_path
    return f"{_status(d)}  {path} (+{d.additions}/-{d.deletions})"


def diff_churn(diffs: Sequence[FileDiff]) -> int:
    """总 churn（新增+删除行数），小改动判定的依据。"""
    return sum(d.additions + d.deletions for d in diffs)


def group_by_files(diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
    """per-file 降级：每个文件一个独立组（组内隔离、组间可并发）。"""
    return [[d] for d in diffs]


def bundle_all(diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
    """整包单组：小改动不值得分组。"""
    return [list(diffs)]


def _split_oversized(
    groups: Sequence[list[FileDiff]], max_files: int
) -> list[list[FileDiff]]:
    """把超过 max_files 的组按序切成多组（强约束切超大组）。"""
    out: list[list[FileDiff]] = []
    for g in groups:
        for i in range(0, len(g), max_files):
            out.append(g[i : i + max_files])
    return out


def _split_budget_overrun(
    groups: Sequence[list[FileDiff]], max_churn: int
) -> list[list[FileDiff]]:
    """每组 churn 超预算 → 拆成单文件组（超大文件自身保持一组）。"""
    out: list[list[FileDiff]] = []
    for g in groups:
        if diff_churn(g) <= max_churn:
            out.append(g)
            continue
        oversized: list[FileDiff] = []
        for d in g:
            if d.additions + d.deletions > max_churn:
                # 单文件就超预算：它无法再拆，保持为一组
                out.append([d])
            else:
                oversized.append(d)
        if oversized:
            # 剩余小文件各自独立成组，避免它们挤在一起又超预算
            out.extend(group_by_files(oversized))
    return out


class GroupLLM(Protocol):
    """分组 LLM 接口：只看文件元数据，返回路径分组结果。

    返回 None 表示不可行/失败（触发 per-file 降级）。
    """

    async def group_metadata(self, entries: list[str], max_files: int) -> list[list[str]] | None:  # noqa: E501
        ...


def _short_circuit(diffs: Sequence[FileDiff]) -> list[list[FileDiff]] | None:
    """本地短路：len≤1 → per-file；files<4 且 churn<200 → 整包单组。

    命中返回对应分组，否则返回 None（表示需要走 LLM 语义分组）。
    """
    if len(diffs) <= 1:
        return group_by_files(diffs)
    if len(diffs) < SMALL_FILE_THRESHOLD and diff_churn(diffs) < SMALL_CHURN_THRESHOLD:
        return [list(diffs)]
    return None


def _group_diffs_by_paths(
    diffs: Sequence[FileDiff], groups: Sequence[Sequence[str]]
) -> list[list[FileDiff]]:
    """按 LLM 返回的路径分组，把 FileDiff 归入对应组。

    LLM 可能漏交某些文件——未被任何组引用的文件按原顺序补成自己的组，
    防止被静默丢弃。
    """
    by_path: dict[str, FileDiff] = {d.new_path or d.old_path: d for d in diffs}
    consumed: set[str] = set()
    out: list[list[FileDiff]] = []
    for paths in groups:
        g = [by_path[p] for p in paths if p in by_path]
        if g:
            out.append(g)
            consumed.update(d.new_path or d.old_path for d in g)
    for d in diffs:
        if (d.new_path or d.old_path) not in consumed:
            out.append([d])
    return out


def _looks_like_per_file(groups: Sequence[Sequence[str]]) -> bool:
    """没有任何组实际并了 ≥2 个文件 → 等于 per-file，不值得当作 LLM 结果。"""
    return all(len(paths) <= 1 for paths in groups)


def decide_grouping(
    diffs: Sequence[FileDiff],
    *,
    max_files: int = MAX_FILES_PER_GROUP,
    group_churn_budget: int = GROUP_CHURN_BUDGET,
) -> list[list[FileDiff]]:
    """无 LLM 的同步决策：短路命中直接返回；否则降级 per-file 再套强约束。

    供测试与小改动路径使用；要真正调 LLM 语义分组走 `SemanticGrouper.group()`。
    """
    short = _short_circuit(diffs)
    if short is not None:
        return short
    grouped = group_by_files(diffs)
    grouped = _split_oversized(grouped, max_files)
    return _split_budget_overrun(grouped, group_churn_budget)


class SemanticGrouper:
    """LLM 语义分组的异步编排：元数据入参 → 分组 → 降级套强约束。

    用一个最便宜的元数据 prompt 调用把关联文件并进一组，失败降级 per-file。
    """

    def __init__(
        self,
        llm: GroupLLM,
        *,
        max_files: int = MAX_FILES_PER_GROUP,
        group_churn_budget: int = GROUP_CHURN_BUDGET,
    ) -> None:
        self._llm = llm
        self._max_files = max_files
        self._group_churn_budget = group_churn_budget

    async def group(self, diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
        """整条异步决策链（含 LLM 语义分组与降级），供编排主链调用。"""
        short = _short_circuit(diffs)
        if short is not None:
            return short

        entries = [format_diff_entry(d) for d in diffs]
        try:
            groups = await self._llm.group_metadata(entries, self._max_files)
        except Exception:  # noqa: BLE001  # LLM 失败 → 降级 per-file（DESIGN §7.2 步骤 4）
            logger.warning("分组 LLM 失败，降级 per-file 分组", exc_info=True)
            groups = None
        # LLM 不可行（None）/ 返回空 / 近乎 per-file → 降级
        if not groups or _looks_like_per_file(groups):
            grouped = group_by_files(diffs)
        else:
            grouped = _group_diffs_by_paths(diffs, groups)
            if not grouped:  # 路径全对不上 → 同样降级
                grouped = group_by_files(diffs)

        # 收尾强约束：先切超 size 组，再拆预算超限组
        grouped = _split_oversized(grouped, self._max_files)
        grouped = _split_budget_overrun(grouped, self._group_churn_budget)
        return grouped

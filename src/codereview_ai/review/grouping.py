"""文件名组编排：与 OCR `internal/agent/grouping.go` 逐条对齐（DESIGN §7.2）。

OCR 的决策链（`Template.GroupingPlan` → `groupDiffs`）：
  1. `len(diffs) <= 1`             → per-file（单文件无需分组，不调 LLM）
  2. `files < 4 且 churn < 200`     → **整包单组**（OCR GroupingBundleAll）
  3. `files < 4 且 churn ≥ 200`     → **per-file**（OCR GroupingPerFile）
  4. `files ≥ 4`                   → 调 LLM 语义分组（只看文件元数据，不含 diff 内容）
       - LLM 失败 / 空响应           → **per-file**（`grouping.go:98` 的降级）
       - 解析结果：重复文件只算一次、未知路径丢弃、**未被任何组引用的文件各成一组**
       - 强约束一：组内文件数 > 10 按序切分（`enforceMaxFilesPerGroup`）
       - 强约束二：组内 diff 累计 token 超过 `PromptTokenLimit(MaxTokens)`
         （= 0.8 × MaxTokens）的组**整体拆成 per-file**（`enforceGroupTokenBudget`）

与 OCR 的唯一有意差异：`group_metadata` 失败会**重试一次**（OCR 直接降级 per-file）。
实测网关空响应/超时多为瞬时，重试只影响失败路径，不改变成功路径的分组形态。

LLM 分组是可注入接口（`GroupLLM`），离线测试用 fake；网络只出现在该接口实现里。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from codereview_ai.domain.models import ChangeType, FileDiff

logger = logging.getLogger("codereview_ai.grouping")

#: 每组文件数硬上限（OCR maxFilesPerGroup=10）：既约束 LLM，也用于切超大组
MAX_FILES_PER_GROUP = 10
#: 小改动阈值：文件 < 4 且总 churn < 200 → 整包单组（OCR GroupingMinFiles / BundleLineThreshold）
SMALL_FILE_THRESHOLD = 4
SMALL_CHURN_THRESHOLD = 200
#: 组内 diff token 预算比例（OCR tokenWarningThreshold：PromptTokenLimit = 0.8 × MaxTokens）
GROUP_TOKEN_BUDGET_RATIO = 0.8
#: 默认组 token 预算：按 AgentConfig.max_prompt_tokens 默认 200k 折算（OCR 同口径）
DEFAULT_GROUP_TOKEN_BUDGET = int(200_000 * GROUP_TOKEN_BUDGET_RATIO)
#: diff 文本 → token 的粗估系数，与 llmloop.estimate_tokens 同口径（OCR 用真 tokenizer）
_CHAR_PER_TOKEN = 4

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


def estimate_diff_tokens(diff_text: str) -> int:
    """diff 文本的 token 粗估（OCR `llm.CountTokens(d.Diff)` 的等价量级）。"""
    return max(1, len(diff_text or "") // _CHAR_PER_TOKEN)


def group_by_files(diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
    """per-file：每个文件一个独立组（组内隔离、组间可并发）。"""
    return [[d] for d in diffs]


def bundle_all(diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
    """整包单组：小改动不值得分组（OCR GroupingBundleAll）。"""
    return [list(diffs)]


def _short_circuit(diffs: Sequence[FileDiff]) -> list[list[FileDiff]] | None:
    """OCR `Template.GroupingPlan` 的等价：本地可定的分组直接返回，None = 要调 LLM。

    OCR 在 `files < GroupingMinFiles` 时**一律不调 LLM**，用 churn 决定整包还是 per-file；
    只有 `files ≥ GroupingMinFiles` 才走语义分组。
    """
    if len(diffs) <= 1:
        return group_by_files(diffs)
    if len(diffs) < SMALL_FILE_THRESHOLD:
        return (
            bundle_all(diffs)
            if diff_churn(diffs) < SMALL_CHURN_THRESHOLD
            else group_by_files(diffs)
        )
    return None


def _split_oversized(
    groups: Sequence[list[FileDiff]], max_files: int
) -> list[list[FileDiff]]:
    """组内文件数 > max_files 时按序切分（OCR enforceMaxFilesPerGroup）。"""
    out: list[list[FileDiff]] = []
    for g in groups:
        if len(g) <= max_files:
            out.append(g)
            continue
        for i in range(0, len(g), max_files):
            out.append(g[i : i + max_files])
    return out


def _enforce_group_token_budget(
    groups: Sequence[list[FileDiff]], token_limit: int
) -> list[list[FileDiff]]:
    """组内 diff 累计 token 超预算 → 整组拆成 per-file（OCR enforceGroupTokenBudget）。

    这是「prompt 体积阀门」：一个组塞太多大文件会顶爆该组会话的 prompt，
    OCR 的处理是放弃合组、退化成每文件一组。
    """
    if token_limit <= 0:
        return list(groups)
    out: list[list[FileDiff]] = []
    for g in groups:
        total = sum(estimate_diff_tokens(d.diff) for d in g)
        if total <= token_limit:
            out.append(g)
        else:
            logger.info(
                "分组 token 预算（%d token）超限：组内 %d 个文件（合计 %d token）拆成 per-file",
                token_limit, len(g), total,
            )
            out.extend(group_by_files(g))
    return out


def filter_oversized_diffs(
    diffs: Sequence[FileDiff],
    token_limit: int = DEFAULT_GROUP_TOKEN_BUDGET,
) -> list[FileDiff]:
    """丢弃**单个文件**的 diff 就超过预算的文件（OCR `filterLargeDiffs`）。

    OCR 在分组之前先做这一步：一个巨大文件的 diff 单独就能顶爆该组会话的 prompt。
    实测某样本（3 文件整包一组、其中一个是巨大的生成文件）请求达 133 万 token，
    超过模型 104 万上限直接失败——这一步就是那个「体积阀门」。
    被跳过的文件会打日志（OCR 同款行为：跳过而非报错）。
    """
    if token_limit <= 0:
        return list(diffs)
    kept: list[FileDiff] = []
    dropped: list[str] = []
    for d in diffs:
        if estimate_diff_tokens(d.diff) > token_limit:
            dropped.append(d.new_path or d.old_path)
        else:
            kept.append(d)
    if dropped:
        logger.warning(
            "diff 超预算（>%d token）已跳过 %d 个文件（OCR filterLargeDiffs）：%s",
            token_limit, len(dropped), dropped[:8],
        )
    return kept


class GroupLLM(Protocol):
    """分组 LLM 接口：只看文件元数据，返回路径分组结果。

    返回 None 表示不可行/失败（触发 per-file 降级）。
    """

    async def group_metadata(self, entries: list[str], max_files: int) -> list[list[str]] | None:  # noqa: E501
        ...


def _group_diffs_by_paths(
    diffs: Sequence[FileDiff], groups: Sequence[Sequence[str]]
) -> list[list[FileDiff]]:
    """按 LLM 返回的路径分组，把 FileDiff 归入对应组（OCR parseGroupingResponse 的等价）。

    与 OCR 同规约：重复出现的文件只归第一个引用它的组、未知路径丢弃、
    **未被任何组引用的文件按原顺序各成一组**（防静默丢文件）。
    """
    by_path: dict[str, FileDiff] = {d.new_path or d.old_path: d for d in diffs}
    consumed: set[str] = set()
    out: list[list[FileDiff]] = []
    for paths in groups:
        g = [by_path[p] for p in paths if p in by_path and p not in consumed]
        if g:
            out.append(g)
            consumed.update(d.new_path or d.old_path for d in g)
    for d in diffs:
        if (d.new_path or d.old_path) not in consumed:
            out.append([d])
    return out


def decide_grouping(
    diffs: Sequence[FileDiff],
    *,
    max_files: int = MAX_FILES_PER_GROUP,
    group_token_budget: int = DEFAULT_GROUP_TOKEN_BUDGET,
) -> list[list[FileDiff]]:
    """无 LLM 时的分组：只可能是 OCR 的两种本地形态（整包 / per-file）+ 收尾约束。

    供测试与小改动路径使用；`files ≥ 4` 需要语义分组时走 `SemanticGrouper.group()`
    （没有 LLM 可用的调用方按 OCR 的降级语义退 per-file）。
    """
    short = _short_circuit(diffs)
    grouped = short if short is not None else group_by_files(diffs)
    grouped = _split_oversized(grouped, max_files)
    return _enforce_group_token_budget(grouped, group_token_budget)


class SemanticGrouper:
    """LLM 语义分组的异步编排：元数据入参 → 分组 → 收尾强约束（OCR `groupDiffs` 的等价）。

    用一次最便宜的元数据 prompt 把关联文件并进一组；失败/空响应降级 per-file。
    """

    def __init__(
        self,
        llm: GroupLLM,
        *,
        max_files: int = MAX_FILES_PER_GROUP,
        group_token_budget: int = DEFAULT_GROUP_TOKEN_BUDGET,
    ) -> None:
        self._llm = llm
        self._max_files = max_files
        self._group_token_budget = group_token_budget

    async def group(self, diffs: Sequence[FileDiff]) -> list[list[FileDiff]]:
        """整条异步决策链（含 LLM 语义分组与降级），供编排主链调用。"""
        short = _short_circuit(diffs)
        if short is not None:
            # OCR groupWithoutLLM 对 BundleAll/PerFile 也套 enforceMaxFilesPerGroup +
            # enforceGroupTokenBudget：小改动整包里若混着巨大文件，必须在这里拆掉，
            # 否则整组的 prompt 会顶爆模型上下文（实测过一次直接失败）。
            grouped = _split_oversized(short, self._max_files)
            return _enforce_group_token_budget(grouped, self._group_token_budget)

        entries = [format_diff_entry(d) for d in diffs]
        groups: list[list[str]] | None = None
        for attempt in (1, 2):
            try:
                groups = await self._llm.group_metadata(entries, self._max_files)
                if groups:
                    break
            except Exception:  # noqa: BLE001  # 空响应/超时多为瞬时（实测网关偶发）
                groups = None
                logger.warning(
                    "分组 LLM 失败（第 %d 次）：%s",
                    attempt,
                    "重试一次" if attempt == 1 else "转 per-file 降级",
                    exc_info=True,
                )
        if not groups:
            # OCR：LLM 不可行 / 空响应 → per-file（不做装箱）
            logger.warning("分组 LLM 不可用，降级 per-file（%d 个文件）", len(diffs))
            return _enforce_group_token_budget(
                group_by_files(diffs), self._group_token_budget
            )

        # 解析结果按 OCR 规约归组（重复/未知路径丢弃、未覆盖文件各成一组）。
        # **LLM 若判定各文件互不相关（等于 per-file）也照用**——这正是 OCR 的行为；
        # 旧实现会把 per-file 结果判为「不值得当 LLM 结果」并改写成装箱，导致多文件
        # 共享同一份会话预算、召回明显下降：某 34 文件样本上只对 8 个文件产出评论，
        # 照用 per-file 结果后覆盖 8→18、语义命中 6→11（同配置重复实验的均值）。
        grouped = _group_diffs_by_paths(diffs, groups) or group_by_files(diffs)
        grouped = _split_oversized(grouped, self._max_files)
        return _enforce_group_token_budget(grouped, self._group_token_budget)


async def group_diffs(
    diffs: Sequence[FileDiff],
    grouper: SemanticGrouper | None,
    *,
    group_token_budget: int = DEFAULT_GROUP_TOKEN_BUDGET,
) -> list[list[FileDiff]]:
    """两条审查轨道共用的分组入口（OCR `groupDiffs` 的等价）。

    `grouper is None`（未配 LLM）时按 OCR 的降级语义：短路命中用短路结果，否则 per-file
    ——绝不静默退化成「整包一组」（那会让多个文件共享同一份会话预算）。
    """
    usable = filter_oversized_diffs(diffs, group_token_budget)
    if not usable:
        logger.warning("所有 diff 都超过单文件预算，本轮无可审文件")
        return []
    if grouper is None:
        return decide_grouping(usable, group_token_budget=group_token_budget)
    return await grouper.group(usable)

"""增量审查选择：last_reviewed_sha 状态 + 内容指纹去重（DESIGN §7.3，M3 用内存档）。

M4 前先落**内存态**（M4→DB）。职责只做「决定」，不做 diff 切片：
  - `decide_increment` —— 按上次审查头 + 当前 base 链是否有效，决定本次走增量还是全量。
  - `dedup_findings` —— 对跨轮 finding 做**内容指纹**（`hash(file + body.lower())`，非行号，
    行号会漂内容不会）去重，避免追加 commit 时重复报上次的问题（DESIGN §7.3 / §13.3）。

行级评论的行号校验仍按**整体** diff 的可评论行集合做（DESIGN §7.3：「两套 diffparse」中
算行号那套），此处不涉及——定位/校验留在 review/location.py。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from codereview_ai.domain.models import Finding, PullRequest

#: 决策原因：供日志与测试断言
REASON_FIRST = "first_review"  # 首次审查无条件全量
REASON_ALREADY = "already_reviewed"  # 当前 head 已审过，跳过
REASON_INCREMENTAL = "incremental"  # 上次 head 在本次 base 链上 → 只审增量
REASON_CHAIN_INVALID = "base_chain_invalid"  # force-push/rebase → 回退全量


def finding_fingerprint(f: Finding) -> str:
    """finding 的身份指纹：`file + ':' + content` 归一后哈希（非行号，内容不漂）。"""
    body = (f.content or "").strip().lower()
    return hashlib.sha1(f"{f.file}:{body}".encode()).hexdigest()


@dataclass(frozen=True)
class IncrementReference:
    """上次成功审查的落点：head_sha + 该轮 findings 的内容指纹集。"""

    head_sha: str
    fingerprints: frozenset[str] = frozenset()


@dataclass
class IncrementDecision:
    """本次审查的增量决定。"""

    is_incremental: bool
    last_reviewed_sha: str | None
    reason: str


class IncrementStore:
    """进程内（provider, pr_number）→ 上次审查落点。M4 换 DB 持久化。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], IncrementReference] = {}

    def last(self, provider: str, pr_number: int) -> IncrementReference | None:
        return self._entries.get((provider, pr_number))

    def record(
        self,
        provider: str,
        pr_number: int,
        head_sha: str,
        fingerprints: frozenset[str] = frozenset(),
    ) -> None:
        self._entries[(provider, pr_number)] = IncrementReference(head_sha, fingerprints)


def decide_from_ref(
    ref: IncrementReference | None,
    pr: PullRequest,
    *,
    chain_valid: bool,
) -> IncrementDecision:
    """按上次审查落点 `ref` 决定本次审查路径。

    `chain_valid` = 上次 head 仍落在本次 PR 的可比较 diff 链上（由平台 compare 校验）。
    force-push / rebase / merge-main 使 last.head_sha 脱离链 → chain_valid=False → 回退全量。
    """
    if ref is None:
        return IncrementDecision(False, None, REASON_FIRST)
    if ref.head_sha == pr.head_sha:
        # 同一 commit 再收到 webhook（重放/去重）→ 已审过，跳过
        return IncrementDecision(False, ref.head_sha, REASON_ALREADY)
    if chain_valid:
        return IncrementDecision(True, ref.head_sha, REASON_INCREMENTAL)
    # 保守回退：宁可全量多审一遍，也不在失效链上误判已解决（DESIGN §7.3）
    return IncrementDecision(False, ref.head_sha, REASON_CHAIN_INVALID)


def decide_increment(
    store: IncrementStore,
    pr: PullRequest,
    *,
    chain_valid: bool,
) -> IncrementDecision:
    """从进程内 store 取上轮落点后走 `decide_from_ref`（兼容 M3 内存档调用方）。"""
    ref = store.last(pr.provider, pr.pr_number)
    return decide_from_ref(ref, pr, chain_valid=chain_valid)


def dedup_findings(findings: list[Finding], ref: IncrementReference | None) -> list[Finding]:
    """按内容指纹滤掉上次已报过的 finding；无上次参考则原样返回。

    指纹不相容（上次没存指纹）时不误删 → 保守原样。
    """
    if ref is None or not ref.fingerprints:
        return findings
    return [f for f in findings if finding_fingerprint(f) not in ref.fingerprints]


def collect_fingerprints(findings: list[Finding]) -> frozenset[str]:
    """把一轮 findings 归一成指纹集，供 `record` 落到 store。"""
    return frozenset(finding_fingerprint(f) for f in findings)

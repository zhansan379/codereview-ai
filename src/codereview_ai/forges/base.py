"""ForgeAdapter 抽象：平台无关的 PR 解析 + 变更/评论回写接口（DESIGN §9）。

实现者把各平台 Webhook payload / REST API 归一成中立领域模型
（PullRequest / FileDiff / ChangeType），上层（worker 审查编排）只依赖本接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from codereview_ai.domain.models import ChangeType, FileDiff, PullRequest, PushEvent

#: 触发审查的事件动作白名单（open/update 语义，跨平台归一）。
REVIEW_ACTIONS = frozenset({"open", "opened", "reopen", "reopened", "update", "synchronize"})


def change_type_from_flags(*, is_new: bool, is_deleted: bool, is_renamed: bool) -> ChangeType:
    if is_deleted:
        return ChangeType.DELETED_FILE
    if is_new:
        return ChangeType.NEW_FILE
    if is_renamed:
        return ChangeType.RENAMED_FILE
    return ChangeType.MODIFIED


def count_diff_stats(diff: str) -> tuple[int, int]:
    """从 unified diff 文本粗略统计 (additions, deletions)——供过滤/展示用。"""
    adds = dels = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


class ForgeAdapter(ABC):
    """平台适配器。HTTP 客户端在构造时注入，测试可用 httpx.MockTransport。"""

    name: str = ""

    @abstractmethod
    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        """从 webhook 的 merge_request 事件解析出中立 PullRequest。"""

    @staticmethod
    def should_review(action: str) -> bool:
        return action in REVIEW_ACTIONS

    async def fetch_pull_request(self, pr: PullRequest) -> PullRequest:
        """按需补齐 PR 元数据（diff_refs / 标题 / 作者），默认原样返回。"""
        return pr

    async def list_open_pulls(self, repo_id: str) -> list[PullRequest]:
        """主动补拉：列出仓库当前**打开**状态的 PR/MR（不依赖 webhook，DESIGN §9 补拉通道）。

        非抽象默认返回空——未实现此能力（如推送轨专用的分析/测试子类）直接留白，
        补拉对该仓库自然跳过而非报错。实现者用 `repo_id` 定位仓库并逐项归一成中立
        `PullRequest`（字段口径与 `parse_merge_request` 一致，含 head_sha/base_sha/
        diff_refs——GitLab 行级评论 position 依赖后者）。
        """
        return []

    @abstractmethod
    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        """拉取 PR 涉及文件的 diff（含 full new_file_content 则更好）。"""

    @abstractmethod
    async def post_summary(self, pr: PullRequest, body: str) -> None:
        """发总结评论。"""

    @abstractmethod
    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        """并行发行级评论。"""

    # ── push 轨（§7.7）：非抽象默认，未实现的分析/测试子类可只保 MR 轨 ──
    def parse_push_event(self, data: dict[str, Any]) -> PushEvent | None:
        """从 webhook 的 push 事件解析出中立 PushEvent；不支持/非 push 返回 None。"""
        return None

    async def get_push_changes(self, ev: PushEvent) -> list[FileDiff]:
        """compare 差量（before→after）。"""
        raise NotImplementedError

    async def get_first_commit_changes(self, ev: PushEvent) -> list[FileDiff]:
        """新分支：only 首个提交的差量。"""
        raise NotImplementedError

    async def post_commit_summary(self, ev: PushEvent, text: str) -> None:
        """push 总结回写到 head commit（MR 轨用 post_summary，push 无 MR 可挂）。"""
        raise NotImplementedError

    async def post_commit_status(
        self, pr: PullRequest, *, passed: bool, description: str = ""
    ) -> None:
        """设一条 head commit 的 CI status（F3.7）：低于阈值 blocked 时 passed=False。

        只传语义布尔 `passed`，各平台自行映射成 state（GitLab success/failed，
        GitHub success/failure）。未实现的分析/测试子类可不覆盖。
        """
        raise NotImplementedError

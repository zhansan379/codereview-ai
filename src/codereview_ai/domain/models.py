"""平台无关的中立领域模型（DESIGN §4）。

所有平台适配器（ForgeAdapter）都输出/消费这些模型，而非各自定义一份。
- `ChangeType/Category/Severity` 为枚举，非法值统一归一/降级而非失败。
- `FileDiff/PullRequest/PushEvent/CommitInfo` 为不可变 `@dataclass(frozen=True)`。
- `Finding` 的 `existing_code` 是 LLM 贴的代码片段（定位锚点，替代行号）；
  `line` 由工程锚定（见 review/location.py）填充，LLM 不产出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ChangeType(StrEnum):
    NEW_FILE = "new"
    DELETED_FILE = "deleted"
    RENAMED_FILE = "renamed"
    MODIFIED = "modified"


@dataclass(frozen=True)
class FileDiff:
    old_path: str
    new_path: str
    diff: str  # 该文件的 unified diff 文本
    additions: int
    deletions: int
    change_type: ChangeType
    # 新文件全文：给定位的"全文兜底 / 跨文件迁移"用（DESIGN §7.1）
    new_file_content: str = ""


@dataclass(frozen=True)
class PullRequest:
    provider: str  # "gitlab" | "github" | ...
    repo_id: str  # 平台内项目唯一 id
    repo_full_name: str  # 展示用 "owner/name"
    web_url: str  # MR/PR 页面 URL
    pr_number: int
    title: str
    source_branch: str
    target_branch: str
    head_sha: str  # 幂等键之一
    base_sha: str
    diff_refs: dict[str, object] | None = None  # gitlab：base/head/start sha，position 必填  # noqa: E501
    author: str = ""
    is_draft: bool = False


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str
    author_name: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class PushEvent:
    provider: str
    repo_id: str
    repo_full_name: str
    branch: str
    before: str  # 全 0 = 新分支
    after: str  # 全 0 = 删分支
    commits: list[CommitInfo] = field(default_factory=list)
    pusher: str = ""


class Category(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TEST = "test"
    STYLE = "style"
    DOCUMENTATION = "documentation"
    OTHER = "other"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Finding:
    # ── 取自 LLM ──
    content: str
    category: Category
    severity: Severity
    existing_code: str  # 定位锚点，替代行号
    file: str  # 相对仓库根
    suggestion_code: str | None = None
    thinking: str | None = None  # 仅审计用，不回写
    # ── 由工程锚定/校验填充，LLM 不产出 ──
    line: int | None = None  # 新侧行号（锚定解析结果）
    old_line: int | None = None  # 旧侧行号（仅新增行类锚定到旧侧时）
    side: str = "RIGHT"  # "RIGHT"(新侧) | "LEFT"(旧侧/删除)，回写 position 用
    source: str = "llm"  # "llm" | "static:<tool>"


@dataclass
class ReviewScores:
    correctness: int = 0
    security: int = 0
    practices: int = 0
    performance: int = 0
    commit_quality: int = 0

    @property
    def total(self) -> int:
        return self.correctness + self.security + self.practices + self.performance + self.commit_quality  # noqa: E501


@dataclass
class ReviewResult:
    summary: str = ""
    scores: ReviewScores = field(default_factory=ReviewScores)
    findings: list[Finding] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    raw_llm_json: dict[str, object] = field(default_factory=dict)

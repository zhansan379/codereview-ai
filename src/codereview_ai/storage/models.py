"""SQLAlchemy ORM 模型（对齐 DESIGN §5 ERD）。

M1 落地三张核心表：`project` / `review_task` / `review_finding`（task 与 finding 拆表，
支撑 idempotency、增量对账与状态机）。其余表（model_config / model_usage / notifier_config /
project_rule）随 M2/M4 的迁移加入。

幂等靠**部分唯一索引**：两轨（mr/push）约束列集不同，不能用表级 UNIQUE，见 DESIGN §5。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (
        Index("uq_project_provider_repo_id", "provider", "repo_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    repo_id: Mapped[str] = mapped_column(String(255))
    repo_full_name: Mapped[str] = mapped_column(String(255), default="")
    web_url: Mapped[str] = mapped_column(String(1024), default="")
    branch_rule: Mapped[str] = mapped_column(String(255), default="")
    file_extensions: Mapped[str] = mapped_column(String(255), default="")
    review_strategy: Mapped[str] = mapped_column(String(32), default="diff")
    prompt_suffix: Mapped[str] = mapped_column(Text, default="")
    score_threshold: Mapped[int] = mapped_column(Integer, default=80)
    notifier_routing: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReviewTask(Base):
    __tablename__ = "review_task"
    # 两轨部分唯一索引：mr 轨 pr_number 非空、push 轨为 NULL（DESIGN §5）
    __table_args__ = (
        Index(
            "uq_review_mr",
            "provider", "repo_id", "pr_number", "head_sha",
            unique=True,
            sqlite_where=text("event_type = 'mr'"),
            postgresql_where=text("event_type = 'mr'"),
        ),
        Index(
            "uq_review_push",
            "provider", "repo_id", "event_type", "branch", "head_sha",
            unique=True,
            sqlite_where=text("event_type = 'push'"),
            postgresql_where=text("event_type = 'push'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    repo_id: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # push 轨为 NULL
    event_type: Mapped[str] = mapped_column(String(16), default="mr")
    branch: Mapped[str] = mapped_column(String(255), default="")
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(16), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    model_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    writeback_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    model_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    diff_snapshot: Mapped[str] = mapped_column(Text, default="")
    summary_md: Mapped[str] = mapped_column(Text, default="")
    score_total: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ReviewFinding(Base):
    __tablename__ = "review_finding"
    __table_args__ = (
        Index("idx_finding_task_id", "task_id"),
        Index("idx_finding_fingerprint", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("review_task.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))  # hash(file + body.lower())
    severity: Mapped[str] = mapped_column(String(16), default="low")
    category: Mapped[str] = mapped_column(String(32), default="other")
    file: Mapped[str] = mapped_column(String(255), default="")
    old_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    existing_code: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="llm")
    status: Mapped[str] = mapped_column(String(16), default="active")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0)


class ModelConfig(Base):
    """模型配置（DESIGN §5 ERD）：api_key 以 Fernet 密文落库（§16）。"""

    __tablename__ = "model_config"
    __table_args__ = (Index("uq_model_name", "name", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")  # Fernet 密文
    base_url: Mapped[str] = mapped_column(String(1024), default="")
    temperature: Mapped[float] = mapped_column(default=0.0)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    capabilities: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotifierConfig(Base):
    """推送渠道 + 项目路由（DESIGN §5 ERD / §15）：project_id NULL=全局默认，非空=项目级覆盖。"""

    __tablename__ = "notifier_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))  # dingtalk|feishu|wecom
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    webhook_encrypted: Mapped[str] = mapped_column(Text, default="")  # Fernet 密文
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")  # Fernet 密文（签名密钥）
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at_threshold: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProjectRule(Base):
    """path/glob 追加规则，首个匹配者胜（F5.3 规则引擎，DESIGN §12.2）。"""

    __tablename__ = "project_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, default=0)
    path_glob: Mapped[str] = mapped_column(String(255), default="*")
    rule_text: Mapped[str] = mapped_column(Text, default="")
    system_merge: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)


class ModelUsage(Base):
    """每轮 LLM 请求一条，成本归因与审计（DESIGN §5 / §10）。"""

    __tablename__ = "model_usage"
    __table_args__ = (Index("idx_usage_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=True)  # 可空：非 task 链路的用量
    phase: Mapped[str] = mapped_column(String(32), default="review")
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    cost: Mapped[float] = mapped_column(default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

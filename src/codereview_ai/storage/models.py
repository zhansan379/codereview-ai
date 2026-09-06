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

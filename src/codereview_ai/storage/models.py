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
    # push 轨审查（DESIGN §7.7）：None=继承全局 env 默认；True/False=显式覆盖；glob 非空则覆盖全局分支规则
    push_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    push_branch_globs: Mapped[str] = mapped_column(String(255), default="")
    # MR 轨审查（与 push 对称）：None=继承全局默认；True/False=显式覆盖
    mr_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    review_strategy: Mapped[str] = mapped_column(String(32), default="diff")
    prompt_suffix: Mapped[str] = mapped_column(Text, default="")
    score_threshold: Mapped[int] = mapped_column(Integer, default=80)
    # F3.7：低于阈值发 failed（阻塞合并）的每项目开关；默认关，避免已有默认 80 让所有项目意外阻塞
    enforce_score_threshold: Mapped[bool] = mapped_column(Boolean, default=False)
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
    pr_title: Mapped[str] = mapped_column(String(255), default="")  # PR/MR 标题（展示用；push 轨留空）
    # 直达原页 URL：mr 轨为 forge 给出的 MR/PR 页面；push 轨为「{项目 web_url}/commit/{head_sha}」
    web_url: Mapped[str] = mapped_column(String(1024), default="")
    # push 轨提交消息（多行、太长不当标题）；详情页单独展示，不占 pr_title/表格列
    push_commits: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(16), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    model_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    writeback_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    # skipped 分型（留空则非 skipped）：push_disabled | branch_mismatch | branch_deleted
    skip_reason: Mapped[str] = mapped_column(String(32), default="")
    # 手动重试意图：worker 见 force_rerun=true 则绕过幂等预检 + push 门控，强制执行该条再清掉
    force_rerun: Mapped[bool] = mapped_column(Boolean, default=False)
    model_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    diff_snapshot: Mapped[str] = mapped_column(Text, default="")
    summary_md: Mapped[str] = mapped_column(Text, default="")
    score_total: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    # 原始 webhook body；重试时据此回放重新入队（否则 simple 档内存队列不会消费 DB 侧 flip 的 queued）
    payload: Mapped[str] = mapped_column(Text, default="")


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


class ReviewConversation(Base):
    """agentic 原始 LLM 对话逐条落库（对齐 OCR `session/persist.go` 的事件流）。

    每条 = 一次 `llm.chat()/summarize()`：`request_json` 存完整 messages（OpenAI 格式，
    含 tool 消息，故工具调用结果已内嵌）；`response_json` 存 {content, tool_calls, usage}。
    `seq` 每 task 单调递增供展示排序；`phase` 标记 plan/main/…/scoring/compress/loop。
    """

    __tablename__ = "review_conversation"
    __table_args__ = (
        Index("idx_conv_task_seq", "task_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("review_task.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(32), default="loop")
    model: Mapped[str] = mapped_column(String(64), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    request_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    at_all: Mapped[bool] = mapped_column(Boolean, default=False)  # 命中阈值时 @所有人
    at_targets: Mapped[list] = mapped_column(JSON, default=list)  # [{author,mobile,wecom_userid,feishu_open_id}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ForgeConfig(Base):
    """平台接入配置（GitHub/GitLab）：url + 加密 token（DESIGN §16 同 model_config）。"""

    __tablename__ = "forge_config"
    __table_args__ = (Index("uq_forge_provider", "provider", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))  # github | gitlab
    url: Mapped[str] = mapped_column(String(1024), default="")
    token_encrypted: Mapped[str] = mapped_column(Text, default="")  # Fernet 密文
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
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


class ScheduleJob(Base):
    """定时任务（主动补拉 / 日报；DESIGN §9 补拉通道 + M5.7 日报调度）。

    每行 = 一条待调度的定时任务实例，`job_type` 限定现存动作：`poll`（补拉轮询，
    params={interval_seconds}）| `daily`（日报，params={hour}）。DB 为唯一事实源；空表
    时由 env 播种默认两条，此后全由后台「定时任务」页 CRUD 驱动运行时热更。
    """

    __tablename__ = "schedule_job"
    __table_args__ = (Index("uq_sched_name", "name", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))  # 任务标签（展示用，唯一）
    job_type: Mapped[str] = mapped_column(String(32))  # poll | daily
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


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


class AppSetting(Base):
    """全局运行时设置（key-value）。每键一行，供阈值/开关/并发等可热更参数落库。

    由 `init_db.create_all` 自动建表（现有库重启即补），无外键——只承载标量字符串值。
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")

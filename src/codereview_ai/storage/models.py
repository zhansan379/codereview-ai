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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
        Index("idx_review_task_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    repo_id: Mapped[str] = mapped_column(String(255))
    # 归属项目（RBAC 隔离；存量可空，读侧按 provider+repo_id 兜底）
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # push 轨为 NULL
    event_type: Mapped[str] = mapped_column(String(16), default="mr")
    branch: Mapped[str] = mapped_column(String(255), default="")
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64), default="")
    pr_title: Mapped[str] = mapped_column(String(255), default="")  # PR/MR 标题（展示用；push 轨留空）
    pr_author: Mapped[str] = mapped_column(String(255), default="")  # PR/MR 创建者（push 轨留空）
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
    # 执行态快照（仪表盘 agent/diff 区分与复杂度→成本分析用）。
    # exec_mode=实际跑的路径（agentic 可能因沙箱关/0 条产出降级为 diff，库内一律记真实值）。
    # NULL = 未真正执行审查（skipped/failed/queued/empty 空审不填），统计与展示时排除。
    exec_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    diff_lines: Mapped[int] = mapped_column(Integer, default=0)  # 新增+删除行合计
    chat_rounds: Mapped[int] = mapped_column(Integer, default=0)  # LLM 调用/对话轮数
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)  # 工具调用累计
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
    # 该轮所在文件组的 key（排序后 new_path 逗号连接，见 group_review._group_key）。
    # 空 = 未分组/整组一次（小改动或历史行）；组审查时每轮随 ACTIVE_GROUP 逐卡带上。
    file_group: Mapped[str] = mapped_column(Text, default="")
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
    # @所有人：评分低于 at_threshold 时，该渠道是否额外 @群内全员（各平台原生 @all）
    at_all: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotifierMember(Base):
    """系统级 @成员名单（一行 = 一个真人，含跨平台 ID 别名）。

    `git_username` 是 forge 提交用户名，供 `pr.author` 按渠道解析成该平台认识的
    @ID（命中不到的人只进文案点名，不进 `atMobiles`）。三平台字段各自可空/为空就
    表示该平台没有标识（推送时跳过）。与渠道的绑定关系存 `NotifierRouteMember`。
    """

    __tablename__ = "notifier_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")  # 展示名
    git_username: Mapped[str] = mapped_column(String(128), default="")  # fork 提交用户名
    dingtalk_mobile: Mapped[str] = mapped_column(String(64), default="")  # 钉钉 @ 手机号
    wecom_userid: Mapped[str] = mapped_column(String(128), default="")  # 企微 userid
    feishu_open_id: Mapped[str] = mapped_column(String(128), default="")  # 飞书 open_id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotifierRouteMember(Base):
    """渠道 × 成员 绑定（无外键，沿用本模块风格；删任一侧需显式清理）。"""

    __tablename__ = "notifier_route_member"

    notifier_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(Integer, primary_key=True)


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


class CloneCacheRepo(Base):
    """agentic 审查的本地 bare-clone 缓存注册表（每仓库一行，记录最近拉取信息）。

    由 `create_all` 自动建表。每行 = 一个被同步到 `cache_root/<slug>/` 的缓存仓库，
    供前端「拉取缓存」页列出 / 单删，并由 `CloneCachePruner` 按清除策略自动清理
    超期未拉取的记录（连同其 `.git` 目录）。同步成功经 `LocalCloneRuntime` 埋点
    upsert（更新 head_sha / last_fetched_at）。
    """

    __tablename__ = "clone_cache_repo"
    __table_args__ = (Index("uq_clone_cache_key", "repo_key", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_key: Mapped[str] = mapped_column(String(128))  # slugify_key(owner/name)：唯一的缓存目录名
    provider: Mapped[str] = mapped_column(String(32))
    repo_full_name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    local_path: Mapped[str] = mapped_column(String(1024), default="")
    head_sha: Mapped[str] = mapped_column(String(64), default="")  # 最近一次同步的目标 head
    last_error: Mapped[str] = mapped_column(String(255), default="")  # 上次同步失败信息（预留）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Role(Base):
    """角色（RBAC）：`is_super`（admin 全通）/`is_system`（内置不可删）/`all_projects`
    （项目级权限对所有项目生效）/`builtin_code`（admin|tech_lead|developer|viewer）。
    """

    __tablename__ = "role"
    __table_args__ = (Index("uq_role_name", "name", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    is_super: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    all_projects: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin_code: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    permissions: Mapped[list[Permission]] = relationship(secondary="role_permission")


class Permission(Base):
    """权限点（静态目录）：`code` 唯一、`scope` ∈ {'global','project'}。seed 时写入。"""

    __tablename__ = "permission"
    __table_args__ = (Index("uq_perm_code", "code", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), default="")
    scope: Mapped[str] = mapped_column(String(16), default="global")
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(255), default="")


class RolePermission(Base):
    """角色-权限 junction。"""

    __tablename__ = "role_permission"
    __table_args__ = (
        Index("uq_role_permission", "role_id", "permission_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"))
    permission_id: Mapped[int] = mapped_column(ForeignKey("permission.id", ondelete="CASCADE"))


class User(Base):
    """后台登录用户：`password_hash`=scrypt 密文；`role_id` 指向全局角色。"""

    __tablename__ = "user"
    __table_args__ = (Index("uq_user_username", "username", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    role: Mapped[Role] = relationship("Role", lazy="joined")


class ProjectMember(Base):
    """项目-用户成员关系（项目级隔离）：用户对其有成员关系的项目才可见/可操作项目级权限。"""

    __tablename__ = "project_member"
    __table_args__ = (
        Index("uq_project_member", "project_id", "user_id", unique=True),
        Index("idx_project_member_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WebhookError(Base):
    """Webhook 配置错误记录（持久化提醒）：当平台发来的 webhook 请求路径错误时落库，
    前端轮询展示提醒，用户确认后标记已读。
    """

    __tablename__ = "webhook_error"
    __table_args__ = (
        Index("idx_webhook_error_ack", "acknowledged"),
        Index("idx_webhook_error_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))  # 平台（gitee/github/gitlab/gitea）
    wrong_url: Mapped[str] = mapped_column(String(1024))  # 错误的完整 URL
    correct_url: Mapped[str] = mapped_column(String(1024))  # 正确的 URL（带 /webhook）
    source_ip: Mapped[str] = mapped_column(String(64), default="")  # 来源 IP
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已确认
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

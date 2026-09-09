"""集中配置 + 启动校验（DESIGN §16）。

- env 前缀 `CR_`，读取 `.env`。
- 密钥类配置（`secret_key`/`webhook_secret`/`encryption_key`）**无默认值**，
  未配置即 `SystemExit`（fail-fast）并给出生成命令。
- `encryption_key` 必须是合法 Fernet 密钥（32 字节、urlsafe-base64，44 字符），
  用 `Fernet(key)` 构造成功来校验，防止用错 `secrets.token_urlsafe` 生成的高强度串。
"""

from __future__ import annotations

from typing import Literal

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 必须在启动时配置、缺失即退出的密钥清单（映射到生成命令）
REQUIRED_SECRETS = {
    "secret_key": (
        "CR_SECRET_KEY",
        "python -c 'import secrets;print(secrets.token_urlsafe(48))'",
    ),
    "webhook_secret": (
        "CR_WEBHOOK_SECRET",
        "python -c 'import secrets;print(secrets.token_urlsafe(48))'",
    ),
    "encryption_key": (
        "CR_ENCRYPTION_KEY",
        "python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'",
    ),
    "admin_password": (
        "CR_ADMIN_PASSWORD",
        "python -c 'import secrets;print(secrets.token_urlsafe(24))'",
    ),
}


class Settings(BaseSettings):
    """应用配置。环境变量统一 `CR_` 前缀。"""

    model_config = SettingsConfigDict(
        env_prefix="CR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # —— 密钥（必须显式配置，fail-fast）——
    secret_key: str = ""
    webhook_secret: str = ""
    encryption_key: str = ""
    admin_password: str = ""  # 后台登录口令（单用户，F5.1）

    # —— 运行时 ——
    database_url: str = "sqlite:///./data/app.db"
    queue_backend: Literal["asyncio", "arq"] = "asyncio"
    redis_url: str | None = None
    log_level: str = "INFO"
    openapi_enabled: bool = True
    frontend_dist: str = ""  # 管理台构建产物目录；为空则按仓库根 frontend/dist 推算

    # —— 审查并发与超时（供 worker 使用）——
    max_concurrent_reviews: int = 4
    request_timeout_seconds: float = 60.0

    # —— push 轨审查（§7.7：默认关，避免刷屏）——
    push_review_enabled: bool = False
    push_branch_globs: str = ""  # 逗号分隔 glob，命中才审；空 = enable 时全放行

    # —— MR 轨审查（与 push 对称：默认关；worker 按事件热读全局默认 → 项目覆盖）——
    mr_review_enabled: bool = False

    # —— 静态分析融合（§11：默认开，缺工具自动降级）——
    review_static_enabled: bool = True
    static_workspace_dir: str = ""  # 临时工作区目录；空 = 系统临时目录
    semgrep_rules_dir: str = ""  # 本地规则目录；空=registry(p/ci)优先、失败内置包兜底

    # —— agentic 审查（§12：默认关；开启后勾 agentic 的项目走「clone 全仓→只读工具」——
    agent_review_enabled: bool = False  # 全局总开关；项目级 review_strategy="agentic" 时才真正走
    agent_clone_cache_dir: str = ""  # 仓库 clone 缓存根；空 = 系统临时目录下 codereview-agent-repos
    agent_max_iterations: int = 20  # 单组 agent 会话最大轮数；轮尽会进 grace 收尾轮逼补交
    agent_max_time_seconds: float = 300.0  # 单组会话最大时长（秒），超时进 grace 收尾轮
    agent_max_prompt_tokens: int = 80_000  # 单组提示 token 预算，超预算进 grace 收尾轮
    agent_group_concurrency: int = 4  # 单次审查内多文件组并发跑的上限（有界信号量，防打爆速率）
    agent_plan_enabled: bool = True  # OCR plan 阶段开关（大变更先规划，失败/空只略过）
    agent_relocation_enabled: bool = True  # OCR re_location 钉行重锚开关（无行意见 LLM 兜底重钉）
    agent_review_filter_enabled: bool = True  # OCR review_filter 事实核查开关（删铁证错评）
    agent_scoring_enabled: bool = True  # 评分收尾开关（0-100 卡片，OCR 无，自创）
    agent_plan_line_threshold: int = 300  # plan 门控：单个超大文件 churn≥此值才规划
    agent_plan_group_line_threshold: int = 600  # plan 门控：组累计 churn≥此值才规划（需≥2文件）
    agent_conversation_enabled: bool = True  # 原始 LLM 对话逐条落库（review_conversation 表）
    agent_reuse_enabled: bool = True  # 增量轮未变更文件复用（sha1(new) 相同则不再喂 agent）

    # —— 日报调度（M5.7：本地时区每日整点）——
    daily_report_enabled: bool = True
    daily_report_hour: int = 9  # 每日推送时刻（本地时区，0-23）

    # —— 主动补拉 PR/MR（DESIGN §9 补拉通道）：默认关；开启后按间隔后台轮询启用项目——
    poll_enabled: bool = False
    poll_interval_seconds: int = 3600  # 轮询间隔秒；手动「补拉」按钮不受此开关限制

    # —— 平台 / LLM（可选；未配齐则 worker 不启动，仅 webhook 可入队）——
    gitlab_url: str = ""
    gitlab_token: str = ""
    github_url: str = "https://api.github.com"
    github_token: str = ""
    llm_model: str = ""

    @model_validator(mode="after")
    def _fail_fast(self) -> Settings:
        """校验必备密钥存在、Fernet 密钥格式合法，缺失/非法直接退出。"""
        missing = [attr for attr, (_env, _cmd) in REQUIRED_SECRETS.items() if not getattr(self, attr)]  # noqa: E501
        if missing:
            raise SystemExit(
                "以下配置未设置，无法安全启动：\n"
                + "\n".join(f"  {attr}（环境变量 {env}，生成命令: {cmd}）"
                            for attr, (env, cmd) in REQUIRED_SECRETS.items() if attr in missing)
                + "\n请先设置 3 个密钥后重试。"
            )
        if self.encryption_key:
            try:
                Fernet(self.encryption_key)
            except ValueError as exc:  # 非 32 字节 urlsafe-base64
                raise SystemExit(
                    "CR_ENCRYPTION_KEY 不是合法的 Fernet 密钥（期望 32 字节 urlsafe-base64、44 字符）。\n"  # noqa: E501
                    "请用 Fernet 生成：python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'；\n"  # noqa: E501
                    "不要用 secrets.token_urlsafe 生成。原始错误：" + str(exc)
                ) from exc
        return self

"""建 M4 后台四张表：model_config / notifier_config / project_rule / model_usage。

DESIGN §5 ERD 的其余三张核心表（project/review_task/review_finding）由 M1 的
`create_all` 收敛，此处不动；本迁移只补后台需要的四张表。幂等用 `op.create_index`。

Revision ID: 0001_m4_admin_tables
Revises: (none)
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_m4_admin_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # —— model_config：模型配置，api_key 以 Fernet 密文落库（§16）——
    op.create_table(
        "model_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(1024), nullable=False, server_default=""),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="4096"),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_model_name", "model_config", ["name"], unique=True)

    # —— notifier_config：推送渠道 + 项目路由（project_id NULL=全局默认）——
    op.create_table(
        "notifier_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("webhook_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("secret_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("at_threshold", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # 同行同 channel 允许「全局 + 项目」并存 → 不做表级唯一（DESIGN §5）

    # —— project_rule：path/glob 追加规则，首个匹配者胜（F5.3）——
    op.create_table(
        "project_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("path_glob", sa.String(255), nullable=False, server_default="*"),
        sa.Column("rule_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("system_merge", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )

    # —— model_usage：每轮 LLM 请求一条，成本归因（§10）——
    op.create_table(
        "model_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(32), nullable=False, server_default="review"),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("cost", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_usage_task_id", "model_usage", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_usage_task_id", table_name="model_usage")
    op.drop_table("model_usage")
    op.drop_table("project_rule")
    op.drop_table("notifier_config")
    op.drop_index("uq_model_name", table_name="model_config")
    op.drop_table("model_config")

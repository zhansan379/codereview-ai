"""补平台接入配置：forge_config（GitHub/GitLab url + 加密 token）。

管理后台「设置」页可改平台 token/URL 并热更生效（DESIGN §16 同 model_config 的
Fernet 落库模式），运行时 `ConfigRepository.resolve_forge` 读它，env 优先压 DB。

Revision ID: 0003_forge_config
Revises: 0002_m5_push_index
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_forge_config"
down_revision = "0002_m5_push_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forge_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("url", sa.String(1024), nullable=False, server_default=""),
        sa.Column("token_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_forge_provider", "forge_config", ["provider"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_forge_provider", table_name="forge_config")
    op.drop_table("forge_config")
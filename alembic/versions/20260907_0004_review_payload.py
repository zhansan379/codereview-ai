"""review_task 增加 payload 列：重试时回放原始 webhook body。

simple 档队列（process 内 asyncio）下，「重试」只用 DB 侧把任务翻回 queued 不会让
worker 重新消费；需持久化原始事件，重试时据此重新 enqueue 进内存队列才能真跑。

Revision ID: 0004_review_payload
Revises: 0003_forge_config
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_review_payload"
down_revision = "0003_forge_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_task",
        sa.Column("payload", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("review_task", "payload")
"""补 M5 push 轨：核心三表（project/review_task/review_finding）+ push 唯一索引。

DESIGN §5：`init_db` 的 create_all 只在应用侧兜底；正式迁移走 Alembic。本迁移把
M1–M3 的核心三表（若空库中尚不存在）落进迁移，并建 push 轨幂等索引
`uq_review_push UNIQUE(provider, repo_id, event_type, branch, head_sha)`（§7.7）。
index 用 `inspect` 守卫，避免与 create_all 已建索引的库冲突。

Revision ID: 0002_m5_push_index
Revises: 0001_m4_admin_tables
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from codereview_ai.storage.models import Base

revision = "0002_m5_push_index"
down_revision = "0001_m4_admin_tables"
branch_labels = None
depends_on = None

#: M1–M3 核心表（ORM 里已定义，正式迁移补齐；空库才建，幂等）
_CORE_TABLES = ("project", "review_task", "review_finding")


def _table_exists(insp: sa.Inspector, name: str) -> bool:
    try:
        return insp.has_table(name)
    except Exception:  # pragma: no cover - 方言差异防御
        return False


def upgrade() -> None:
    bind = op.get_bind()
    # 空库（纯 alembic 全新库）先建核心三表，再从 ORM metadata 拖建；已存在则跳过
    for name in _CORE_TABLES:
        if not _table_exists(sa.inspect(bind), name):
            Base.metadata.tables[name].create(bind)
    # push 轨幂等索引（§7.7）；create_all 已建则跳过，避免重复创建报错
    insp = sa.inspect(bind)
    for ix in insp.get_indexes("review_task"):
        if ix.get("name") == "uq_review_push":
            return
    op.create_index(
        "uq_review_push",
        "review_task",
        ["provider", "repo_id", "event_type", "branch", "head_sha"],
        unique=True,
        sqlite_where=sa.text("event_type = 'push'"),
        postgresql_where=sa.text("event_type = 'push'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_push", table_name="review_task")
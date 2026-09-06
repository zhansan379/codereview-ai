"""Alembic 迁移环境（异步应用 → 同步迁移）。

- 数据库 URL 来自 `codereview_ai.config.Settings.database_url`（`CR_DATABASE_URL`），
  迁移用**同步**引擎，故把 async dialect（`sqlite+aiosqlite`/`postgresql+asyncpg`）还原成同步方言。
- `target_metadata` 指向 `Base.metadata`，支持 autogenerate。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from codereview_ai.config import Settings
from codereview_ai.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: 同步方言表：把 async 前缀还原成同步（alembic 用同步链）
_SYNC = (
    ("sqlite+aiosqlite", "sqlite"),
    ("postgresql+asyncpg", "postgresql"),
)


def _sync_url(url: str) -> str:
    for async_prefix, sync_dialect in _SYNC:
        if url.startswith(async_prefix):
            return sync_dialect + url[len(async_prefix):]
    return url


def _database_url() -> str:
    """取配置里的 URL 并转成同步方言；优先级 cmd-line override > Settings。"""
    configured = config.get_main_option("sqlalchemy.url") or ""
    base = configured if configured else Settings().database_url
    return _sync_url(base)


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

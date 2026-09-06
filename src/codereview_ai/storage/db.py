"""Async engine / session —— 双档切换 + SQLite 加固（DESIGN §8.2）。

- 通过 `DATABASE_URL` 切换：`sqlite+aiosqlite:///...`（simple）或
  `postgresql+asyncpg://...`（standard）。
- SQLite 每个连接初始化 `WAL / busy_timeout / foreign_keys`（修复旧项目
  `database is locked` 与 fd 泄漏问题，见 reference/antipatterns.md）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SQLITE_URL_PREFIXES = ("sqlite", "sqlite+aiosqlite", "sqlite+pysqlite")


def _is_sqlite(url: str) -> bool:
    return any(url.startswith(p) for p in SQLITE_URL_PREFIXES)


def create_engine(database_url: str) -> AsyncEngine:
    """按 URL 建 async engine；SQLite 自动加固。"""
    engine = create_async_engine(database_url, pool_pre_ping=True)

    if _is_sqlite(database_url):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            try:
                # WAL 提升并发；busy_timeout 缓和写锁；foreign_keys 保证级联删除
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """建表（M1 冒烟用；正式迁移走 Alembic）。"""
    from codereview_ai.storage.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个 session，yield 后回滚/关闭。"""
    async with session_factory(engine)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

"""Async engine / session —— 双档切换 + SQLite 加固（DESIGN §8.2）。

- 通过 `DATABASE_URL` 切换：`sqlite+aiosqlite:///...`（simple）或
  `postgresql+asyncpg://...`（standard）。
- SQLite 每个连接初始化 `WAL / busy_timeout / foreign_keys`（修复旧项目
  `database is locked` 与 fd 泄漏问题，见 reference/antipatterns.md）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

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


def ensure_async_url(url: str) -> str:
    """把文档里简写的同步 DSN 翻译成 async dialect（DESIGN §8.2 默认 `sqlite:///...`）。"""
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite" + url[len("sqlite"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg" + url[len("postgresql"):]
    return url


def _ensure_sqlite_dir(url: str) -> None:
    """文件型 SQLite（非 :memory:）自动创建父目录，避免 `unable to open database file`。"""
    if ":memory:" in url:
        return
    rest = url.split("///", 1)[-1]
    if "?" in rest:
        rest = rest.split("?", 1)[0]
    if not rest:
        return
    parent = Path(rest).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str) -> AsyncEngine:
    """按 URL 建 async engine；SQLite 自动加固 + 自动建父目录。"""
    url = ensure_async_url(database_url)
    if _is_sqlite(url):
        _ensure_sqlite_dir(url)
    engine = create_async_engine(url, pool_pre_ping=True)

    if _is_sqlite(url):

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


#: create_all 只建新表、不 ALTER 存量表（Alembic 已移除）。此处登记【存量表增量补列】
#: 的列定义：每次启动按 PRAGMA 检查缺失即 ALTER ADD（幂等、非破坏，保留既有数据）。
#: push_enabled 无默认 → 存量行 NULL = 继承全局 env 默认（DESIGN §7.7 项目覆盖）。
_COLUMN_FALLBACKS: dict[str, list[tuple[str, str, str]]] = {
    "project": [
        ("push_enabled", "BOOLEAN", ""),          # 存量行 NULL=继承全局
        ("push_branch_globs", "VARCHAR(255)", "DEFAULT ''"),
        ("enforce_score_threshold", "BOOLEAN", "DEFAULT 0"),  # F3.7 评分阻塞开关
    ],
    "review_task": [
        ("skip_reason", "VARCHAR(32)", "DEFAULT ''"),
        ("force_rerun", "BOOLEAN", "DEFAULT 0"),
        ("pr_title", "VARCHAR(255)", "DEFAULT ''"),
        ("push_commits", "TEXT", "DEFAULT ''"),
        ("web_url", "VARCHAR(1024)", "DEFAULT ''"),
        # 执行态四列（exec_mode=实际路径；agentic 降级 diff 时落 'diff'）
        ("exec_mode", "VARCHAR(16)", "DEFAULT 'diff'"),
        ("diff_lines", "INTEGER", "DEFAULT 0"),
        ("chat_rounds", "INTEGER", "DEFAULT 0"),
        ("tool_calls", "INTEGER", "DEFAULT 0"),
    ],
}


async def _ensure_latest_schema(engine: AsyncEngine) -> None:
    """为存量表补上缺失的新列（SQLite）；幂等。非 SQLite 由 create_all/正式迁移负责。"""
    if not _is_sqlite(str(engine.url)):
        return
    from sqlalchemy import text

    async with engine.begin() as conn:
        for table, cols in _COLUMN_FALLBACKS.items():
            existing = {
                row[1]
                for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            }
            for name, ddl, default in cols:
                if name not in existing:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl} {default}")
                    )


async def init_db(engine: AsyncEngine) -> None:
    """建表（create_all）+ 存量表补列（幂等，保留数据）。"""
    from codereview_ai.storage.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_latest_schema(engine)


async def get_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个 session，yield 后回滚/关闭。"""
    async with session_factory(engine)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise

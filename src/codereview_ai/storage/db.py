"""Async engine / session —— 双档切换 + SQLite 加固（DESIGN §8.2）。

- 通过 `DATABASE_URL` 切换：`sqlite+aiosqlite:///...`（simple）或
  `postgresql+asyncpg://...`（standard）。
- SQLite 每个连接初始化 `WAL / busy_timeout / foreign_keys`（修复旧项目
  `database is locked` 与 fd 泄漏问题，见 reference/antipatterns.md）。
- 建表统一走 `Base.metadata.create_all`（方言编译器生成 DDL），不做存量表补列
  （schema 已稳定；SQLite/PG 首次建表即最新结构，见 init_db）。
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger("codereview_ai.db")

SQLITE_URL_PREFIXES = ("sqlite", "sqlite+aiosqlite", "sqlite+pysqlite")

#: 连接层失败的特征子串（对整条异常链逐环小写匹配，见 is_connect_failure）。
#: 覆盖拒绝 / 超时 / DNS 解析 / 认证失败 / 连接数上限这几类「环境问题」；
#: 其余数据库异常（如 SQL 语义错误）不算，保持原 traceback 便于定位代码问题。
_CONNECT_ERROR_KEYWORDS = (
    "connection refused",
    "could not connect",
    "connection timed out",
    "timed out",
    "getaddrinfo failed",
    "name or service not known",
    "password authentication failed",
    "authentication failed",
    "too many clients",
    "no pg_hba.conf",
)


def is_connect_failure(exc: BaseException) -> bool:
    """判断异常链上是否出现**连接层**失败（拒绝/超时/DNS/认证）。

    asyncpg/SQLAlchemy 会把底层 OSError 层层包裹后抛出，因此沿 `__cause__` /
    `__context__` 链逐环检查：出现 `ConnectionError` 系（含 ConnectionRefusedError）、
    `TimeoutError`、`socket.gaierror` 或命中特征关键字即认定是环境连不上库，
    而非代码/SQL 问题。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (ConnectionError, TimeoutError, socket.gaierror)):
            return True
        text = str(cur).lower()
        if any(k in text for k in _CONNECT_ERROR_KEYWORDS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _auth_failure(exc: BaseException) -> bool:
    """异常链上是否为认证失败（提示语与「服务没起」区分开）。"""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if "authentication failed" in str(cur).lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def raise_for_connect_failure(
    database_url: str, exc: Exception, *, exit_fn=None  # type: ignore[no-untyped-def]
) -> None:
    """启动连不上数据库时的兜底：打印原因与解决方法后干净退出（不甩 traceback）。

    是连接层失败 → logger.error 输出中文排查指引（URL 打码展示），再退出进程。
    退出走 `exit_fn`（默认 `os._exit(1)`）而不是抛 SystemExit：uvicorn 会把从
    lifespan 冒出的 SystemExit 连同内部栈帧一起打成一页 traceback，正违背「兜底
    提醒而非抛错」的本意；`os._exit` 在启动失败点（尚未绑定端口、无需清理）直接
    终止，控制台只剩那段指引。测试注入 fake 验证即可。
    非连接类异常 → 原样返回，由调用方继续抛出（保留完整 traceback）。
    """
    if not is_connect_failure(exc):
        return
    try:
        shown = make_url(database_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — URL 解析失败就打码整个串，绝不让明文密码进日志
        shown = "<unparseable-database-url>"
    reason = "认证失败（用户名或密码不对）" if _auth_failure(exc) else str(exc).strip() or type(exc).__name__
    logger.error(
        "\n".join([
            "数据库连接失败，服务无法启动。",
            "",
            f"  目标：{shown}",
            f"  原因：{reason}",
            "",
            "可按以下任一方式解决：",
            "  A) 切回 SQLite（最快）：在 .env 中注释掉 CR_DATABASE_URL（默认 sqlite:///./data/app.db），重启即可；",
            "  B) 用 Docker 起一个映射了端口的 PostgreSQL，与 .env 的账号/密码/库名对齐：",
            "     docker run -d --name codereview-pg -p 5432:5432 \\",
            "       -e POSTGRES_USER=<用户> -e POSTGRES_PASSWORD=<密码> -e POSTGRES_DB=<库名> \\",
            "       -v codereview_pgdata:/var/lib/postgresql/data postgres:16",
            "  C) 已有数据库实例：确认服务已启动、地址/端口/账号正确。",
            "     注意：docker compose 内的 postgres 服务未映射宿主端口，宿主机直跑 uvicorn 连不上它",
            "     （需给容器加 -p 5432:5432，或让应用也跑进 compose；详见 docs/how_use_postgres.md）。",
        ])
    )
    for h in logging.getLogger().handlers:
        h.flush()
    sys.stderr.flush()
    if exit_fn is None:
        exit_fn = os._exit
    exit_fn(1)


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


async def init_db(engine: AsyncEngine) -> None:
    """建表（create_all，只建缺失表；schema 已稳定，不做存量表补列）。

    SQLite/PG 的 DDL 由方言编译器各自生成；已存在（含旧版本建）的表一律跳过。
    """
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

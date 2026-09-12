"""storage/db 兜底提示测试：连接层失败识别、友好退出、URL 打码。

全程离线（真实拒绝连接用本机保留端口 1，立即 ECONNREFUSED）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from codereview_ai.storage.db import (
    create_engine,
    init_db,
    is_connect_failure,
    raise_for_connect_failure,
)

PG_URL = "postgresql+asyncpg://postgres:s3cret@127.0.0.1:1/codereview"


def _wrapped(cause: Exception, msg: str = "could not connect to server") -> Exception:
    """模拟 SQLAlchemy 把底层错误包成 OperationalError 的异常链。"""
    exc = OperationalError("CONNECT", {}, Exception(msg))
    exc.__cause__ = cause
    return exc


# ── is_connect_failure ──────────────────────────────────────────────────


def test_refused_error_in_chain_is_connect_failure():
    exc = _wrapped(ConnectionRefusedError("[WinError 1225] 远程计算机拒绝网络连接"))
    assert is_connect_failure(exc) is True


def test_keyword_only_message_is_connect_failure():
    exc = _wrapped(None, "password authentication failed for user \"postgres\"")
    assert is_connect_failure(exc) is True


def test_plain_error_is_not_connect_failure():
    assert is_connect_failure(ValueError("boom")) is False
    assert is_connect_failure(_wrapped(None, "syntax error at or near SELCT")) is False


# ── raise_for_connect_failure（main lifespan 的兜底）──────────────────────


def _fake_exit(calls: list[int]):  # type: ignore[no-untyped-def]
    """注入的退出函数：不打死测试进程，记下退出码并模拟进程终止。"""
    def _exit(code: int) -> None:
        calls.append(code)
        raise SystemExit(code)  # 语义上等同进程终止，pytest 可捕获
    return _exit


def test_connect_failure_exits_with_friendly_message(caplog):
    calls: list[int] = []
    exc = _wrapped(ConnectionRefusedError("[WinError 1225] 远程计算机拒绝网络连接"))
    with caplog.at_level("ERROR", logger="codereview_ai.db"):
        with pytest.raises(SystemExit) as ei:
            raise_for_connect_failure(PG_URL, exc, exit_fn=_fake_exit(calls))
    assert calls == [1] and ei.value.code == 1
    text = caplog.text
    assert "数据库连接失败" in text and "方式解决" in text
    assert "sqlite:///./data/app.db" in text          # 解决方法 A 给出默认库
    assert "127.0.0.1:1/codereview" in text           # 目标地址可见（排除被截断）
    assert "s3cret" not in text                       # 密码必须被打码


def test_auth_failure_message_mentions_credentials(caplog):
    calls: list[int] = []
    exc = _wrapped(None, 'password authentication failed for user "postgres"')
    with caplog.at_level("ERROR", logger="codereview_ai.db"):
        with pytest.raises(SystemExit):
            raise_for_connect_failure(PG_URL, exc, exit_fn=_fake_exit(calls))
    assert "认证失败" in caplog.text


def test_non_connect_error_passes_through():
    # 非连接类异常：不退出、不吞——返回 None 让调用方原样上抛
    assert raise_for_connect_failure(PG_URL, ValueError("boom")) is None


# ── 真实拒绝连接（127.0.0.1:1 保留端口，立即拒绝）──────────────────────────


def test_real_refused_connection_is_detected():
    import asyncio

    async def _attempt() -> Exception:
        engine = create_engine(PG_URL)
        try:
            await init_db(engine)
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            await engine.dispose()
        raise AssertionError("init_db should fail")

    exc = asyncio.run(_attempt())
    assert is_connect_failure(exc) is True

"""config 模块单元测试：fail-fast 校验与 Fernet 密钥格式校验。"""

from __future__ import annotations

import base64

import pytest

from codereview_ai.config import Settings


def _valid_fernet_key() -> str:
    """生成一个真实合法的 Fernet key（32 字节 urlsafe-base64 + 固定签名，44 字符）。"""
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


def _env(**kw):

    defaults = dict(
        secret_key="s",
        webhook_secret="w",
        encryption_key=_valid_fernet_key(),
        admin_password="a",
    )
    defaults.update(kw)
    return defaults


def test_valid_settings_construct():
    s = Settings(**_env())
    assert s.secret_key == "s"
    assert s.database_url == "sqlite:///./data/app.db"
    assert s.queue_backend == "asyncio"


def test_missing_secret_fails_fast():
    with pytest.raises(SystemExit) as e:
        Settings(**_env(secret_key="", webhook_secret="", encryption_key=""))
    assert "CR_SECRET_KEY" in str(e.value)


def test_missing_only_encryption_key_fails():
    with pytest.raises(SystemExit) as e:
        Settings(**_env(encryption_key=""))
    assert "CR_ENCRYPTION_KEY" in str(e.value)


def test_invalid_fernet_key_fails():
    with pytest.raises(SystemExit) as e:
        Settings(**_env(encryption_key="not-a-fernet-key"))
    assert "Fernet" in str(e.value)


def test_wrong_length_fernet_key_fails():
    # tokens.token_urlsafe 生成的是 64 字符，不是 Fernet（44 字符）
    bad = base64.urlsafe_b64encode(b"\x00" * 48).decode()
    with pytest.raises(SystemExit):
        Settings(**_env(encryption_key=bad))

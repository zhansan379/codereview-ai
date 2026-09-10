"""密码哈希（RBAC 多用户）：stdlib `hashlib.scrypt` + 每用户盐，零新增依赖。

格式：`scrypt$<N>$<urlsafe_b64(salt)>$<urlsafe_b64(dk)>`。
scrypt 是 CPython 标准库、需 OpenSSL（本机与 CI 均有）；`secrets.compare_digest`
做常量时间比较，防时序侧信道；畸形/未知 scheme 一律返回 False（不裸抛）。
"""

from __future__ import annotations

import base64
import hashlib
import secrets

_ALG = "scrypt"
_N = 2**14
_R = 8
_P = 1
_DKLEN = 64
_SALT_BYTES = 16
MIN_PASSWORD_LEN = 8


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(text.encode("ascii") + b"=" * (-len(text) % 4))
    except (ValueError, TypeError):
        return None


def hash_password(password: str) -> str:
    """生成 `scrypt$N$salt$dk` 密文。每次随机盐 → 同密码不同密文。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"{_ALG}${_N}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。任何格式问题（未知 scheme、缺段、解码失败）→ False。"""
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != _ALG:
        return False
    try:
        n = int(parts[1])
    except ValueError:
        return False
    salt = _unb64(parts[2])
    expected = _unb64(parts[3])
    if salt is None or expected is None:
        return False
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=_R, p=_P,
                            dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(dk, expected)


def reset_meets_policy(password: str) -> bool:
    """密码策略：最小长度 ≥8。创建与重置共用。"""
    return len(password) >= MIN_PASSWORD_LEN

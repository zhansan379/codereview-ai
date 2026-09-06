"""Fernet 加解密 helper（DESIGN §16 密钥加密存储）。

后端把模型 api_key / 通知 webhook 与签名 secret 以 Fernet 密文落库；主密钥来自
`CR_ENCRYPTION_KEY`（启动校验已保证是合法 Fernet 密钥）。读路径统一回显 `******`，
本模块只在写时加密、后台/worker 内部使用时解密。
"""

from __future__ import annotations

from cryptography.fernet import Fernet

#: 读路径对外回显的掩码（DESIGN §16：后台统一回显 ******）
MASK = "******"

_MISSING = object()


def _fernet(key: str | object) -> Fernet:
    if not isinstance(key, str) or not key:
        raise ValueError("encryption_key 未配置，无法加解密密钥类字段")
    return Fernet(key)


def encrypt(plaintext: str, encryption_key: str | None) -> str:
    """加密明文字符串；空明文 / 无密钥 → 原样空串（避免给空值也套密文）。"""
    if not plaintext:
        return ""
    return _fernet(encryption_key or _MISSING).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str, encryption_key: str | None) -> str:
    """解密密文；空密文 → 空串，密钥缺失/损坏 → 抛错（上层决定降级还是 500）。"""
    if not ciphertext:
        return ""
    return _fernet(encryption_key or _MISSING).decrypt(ciphertext.encode("ascii")).decode("utf-8")


def is_masked(plaintext: str) -> bool:
    """后台写请求带 `******` 表示「保留原文不动」，不应重新加密覆盖。"""
    return plaintext == MASK

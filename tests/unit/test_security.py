"""密码哈希（RBAC `security.py`）：往返、错误密码、畸形/未知 scheme 容错、策略。"""

from __future__ import annotations

from codereview_ai.security import hash_password, reset_meets_policy, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$16384$")
    assert verify_password("correct horse", h)


def test_wrong_password_rejected():
    h = hash_password("hunter2")
    assert not verify_password("hunter3", h)
    assert not verify_password("", h)


def test_salt_differs_per_hash():
    assert hash_password("same") != hash_password("same")


def test_malformed_stored_returns_false():
    for bad in ["", "scrypt", "scrypt$1$2", "bcrypt$x$y$z$w", "notahash", "a$b$c$d$e"]:
        assert not verify_password("pw", bad)


def test_reset_meets_policy():
    assert not reset_meets_policy("short")
    assert reset_meets_policy("12345678")
    assert reset_meets_policy("x" * 100)

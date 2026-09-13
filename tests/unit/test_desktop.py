"""桌面版启动器单测：密钥引导 / 数据目录选择 / 前端产物定位 / 端口探测。

离线纯临时目录；冻结态（sys.frozen/_MEIPASS）用 monkeypatch 模拟。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import codereview_ai.desktop as desktop
from codereview_ai.config.settings import REQUIRED_SECRETS
from codereview_ai.desktop import (
    _read_env_keys,
    ensure_env,
    env_value,
    pick_app_dir,
    port_free,
    resolve_frontend_dist,
)

#: REQUIRED_SECRETS 的键是 settings 属性名，启动器按 env 名（values[0]）走
_ENV_NAMES = {env for env, _cmd in REQUIRED_SECRETS.values()}


def test_ensure_env_generates_all_required_secrets(tmp_path: Path) -> None:
    env_path, generated = ensure_env(tmp_path)
    assert env_path == tmp_path / ".env"
    assert set(generated) == _ENV_NAMES

    text = env_path.read_text(encoding="utf-8")
    for env in _ENV_NAMES:
        assert f"{env}=" in text
    # Fernet 密钥必须能过 Settings 同款校验（非法格式会抛异常）
    Fernet(env_value(env_path, "CR_ENCRYPTION_KEY"))


def test_ensure_env_is_idempotent(tmp_path: Path) -> None:
    first_path, _ = ensure_env(tmp_path)
    values = {env: env_value(first_path, env) for env in _ENV_NAMES}

    _, second = ensure_env(tmp_path)

    assert second == []
    for env, value in values.items():
        assert env_value(first_path, env) == value  # 已生成的密钥不被重生成


def test_ensure_env_fills_only_missing_and_keeps_existing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('CR_SECRET_KEY="keep-me"\n', encoding="utf-8")

    _, generated = ensure_env(tmp_path)

    assert set(generated) == _ENV_NAMES - {"CR_SECRET_KEY"}
    assert env_value(env_path, "CR_SECRET_KEY") == '"keep-me"'  # 原行原样保留（含引号）
    assert env_value(env_path, "CR_ADMIN_PASSWORD") != ""


def test_read_env_keys_ignores_unknown_lines(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("CR_LLM_MODEL=x\nFOO=1\nCR_WEBHOOK_SECRET=abc\n", encoding="utf-8")
    assert _read_env_keys(env_path) == {"CR_WEBHOOK_SECRET"}


def test_pick_app_dir_portable_when_exe_dir_writable(tmp_path: Path) -> None:
    exe = tmp_path / "codereview-ai.exe"
    exe.write_bytes(b"")
    monkeypatch_frozen = pytest.MonkeyPatch()
    monkeypatch_frozen.setattr(sys, "frozen", True, raising=False)
    monkeypatch_frozen.setattr(sys, "executable", str(exe))
    try:
        assert pick_app_dir() == tmp_path
    finally:
        monkeypatch_frozen.undo()


def test_pick_app_dir_falls_back_to_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "ro" / "codereview-ai.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(desktop, "_writable", lambda d: False)  # 模拟 Program Files 只读
    la = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(la))

    got = pick_app_dir()

    assert got == la / "codereview-ai"
    assert got.is_dir()


def test_resolve_frontend_dist_frozen_missing(tmp_path: Path) -> None:
    monkeypatch_ctx = pytest.MonkeyPatch()
    monkeypatch_ctx.setattr(sys, "frozen", True, raising=False)
    monkeypatch_ctx.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    try:
        assert resolve_frontend_dist() is None
    finally:
        monkeypatch_ctx.undo()


def test_resolve_frontend_dist_frozen_present(tmp_path: Path) -> None:
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch_ctx = pytest.MonkeyPatch()
    monkeypatch_ctx.setattr(sys, "frozen", True, raising=False)
    monkeypatch_ctx.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    try:
        assert resolve_frontend_dist() == dist
    finally:
        monkeypatch_ctx.undo()


def test_port_free() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    busy_port = sock.getsockname()[1]
    sock.listen(1)
    try:
        assert not port_free("127.0.0.1", busy_port)
    finally:
        sock.close()
    assert port_free("127.0.0.1", busy_port)  # 释放后可再绑

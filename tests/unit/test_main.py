"""main 装配测试：生命周期建库、/health 与 /ready。"""

from __future__ import annotations

import base64
import subprocess
import sys

from fastapi.testclient import TestClient

from codereview_ai.config import Settings
from codereview_ai.main import create_app


def _valid_fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


def test_app_health_and_ready_after_lifespan(tmp_path):
    settings = Settings(
        secret_key="s",
        webhook_secret="w",
        encryption_key=_valid_fernet_key(),
        admin_password="admin",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["db"] == "ok"


def test_app_refuses_to_start_without_keys(tmp_path):
    import os
    from pathlib import Path

    # 子进程里 import main（会构造 Settings），确认缺密钥时 fail-fast 退出。
    # cwd 指到临时目录避免读到仓库根 `.env`（Settings 会自动读 .env），否则测试被本机密钥污染；
    # PYTHONPATH 用绝对路径，不随 cwd 漂移。
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CR_")}
    env["PYTHONPATH"] = str(repo_root / "src")
    result = subprocess.run(
        # 访问惰性 app 触发 Settings 构造，等效 uvicorn main:app 的启动路径
        [sys.executable, "-c", "import codereview_ai.main as m; _ = m.app"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert result.returncode != 0
    assert "CR_SECRET_KEY" in result.stderr or "CR_SECRET_KEY" in result.stdout

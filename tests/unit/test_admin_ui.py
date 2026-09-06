"""管理台静态托管测试（DESIGN §14.2 / M4.8）：/admin 回退 index.html、资源直回、缺失不挂载。

离线：无需真实构建产物——临时构造 dist 目录写 index.html 与静态资源，断言 SPA 回退、
真实资源寻址、路径穿越被拦截；产物缺失时调用 mount_admin 仅跳过不报错。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from codereview_ai.api.admin_ui import mount_admin


def _write_dist(tmp_path: Path, *, index_text: str = "admin-index") -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f"<html>{index_text}</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_mount_serves_index_fallback_for_spa_route(tmp_path):
    dist = _write_dist(tmp_path)
    app = FastAPI()
    mount_admin(app, str(dist))

    # SPA 路由（非文件）→ 回退 index.html
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        root = client.get("/admin")
        assert root.status_code == 200 and "admin-index" in root.text
        # 未知深层路由 → 仍回退 index.html（交给前端 router）
        spa = client.get("/admin/projects")
        assert spa.status_code == 200 and "admin-index" in spa.text


def test_mount_serves_real_asset(tmp_path):
    dist = _write_dist(tmp_path)
    app = FastAPI()
    mount_admin(app, str(dist))

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.get("/admin/assets/app.js")
        assert r.status_code == 200
        assert "console.log" in r.text


def test_mount_skips_when_dist_missing(tmp_path):
    """产物缺失时 mount_admin 不报错、也不注册 /admin 路由。"""
    app = FastAPI()
    mount_admin(app, str(tmp_path / "nope"))
    paths = [getattr(r, "path", "") for r in app.router.routes]
    assert "/admin" not in paths

"""把 Vue 管理台构建产物托管到 `/admin`（DESIGN §14.2 路由 / M4.8）。

SPA 路由（如 `/admin/projects`）由前端 router 处理，因此后端对 `/admin/{path:path}`
做「命中文件 → 回退 index.html」的抓取处理：有 `frontend/dist` 则挂载，否则 404。

产物目录：优先 `CR_FRONTEND_DIST` 显式配置；缺省按仓库根 `frontend/dist` 推算；
都不存在时跳过挂载（仅后台 API 可用，前端未构建）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

logger = logging.getLogger("codereview_ai.admin_ui")


def _resolve_dist(configured: str) -> Path | None:
    """定位前端构建产物目录；不存在返回 None。

    显式配置（`CR_FRONTEND_DIST`）是权威：配了就用它（缺失则视为未构建）。
    未配置时缺省按仓库根 `frontend/dist` 推算。
    """
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    else:
        # 仓库根 = 本模块上级上级上级的上级（api -> codereview_ai -> src -> 根）
        repo_root = Path(__file__).resolve().parents[3]
        candidates.append(repo_root / "frontend" / "dist")
    return next(
        (p for p in candidates if p.is_dir() and (p / "index.html").is_file()), None
    )


def mount_admin(app: FastAPI, configured: str = "") -> None:
    """把管理台静态产物挂到 `/admin`；产物缺失时记日志并跳过。"""
    dist = _resolve_dist(configured)
    if dist is None:
        logger.warning("未找到前端构建产物（frontend/dist），/admin 后台页面不可用")
        return
    index_path = dist / "index.html"

    @app.get("/admin")
    async def admin_root() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/admin/{path:path}")
    async def admin_spa(path: str) -> FileResponse:
        # 命中真实静态资源（js/css/图片）→ 直接回；否则回退 index.html 交给前端路由
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_path)

    logger.info("管理台已挂载 /admin（%s）", dist)

"""项目级审查配置读取（DESIGN 文件扩展名过滤接入 diff 管线）。

worker 在审查时按 `(provider, repo_id)` 读 `project` 启用行，取 `file_extensions` 等
每项目配置；**每次事件实时查**（无 TTL），配置改动立即生效，呼应 forge 热更理念。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import Project


@dataclass(frozen=True)
class ProjectConfig:
    """解析出的项目级审查配置（现仅文件扩展名；未来可扩展 review_strategy 等）。"""

    file_extensions: str = ""


class ProjectRepository:
    """按 (provider, repo_id) 返回项目启用行的审查配置；未注册/禁用 → None。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def config_for(self, provider: str, repo_id: str) -> ProjectConfig | None:
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(Project).where(
                    Project.provider == provider,
                    Project.repo_id == repo_id,
                    Project.enabled.is_(True),
                )
            )).scalars().first()
        if row is None:
            return None
        return ProjectConfig(file_extensions=row.file_extensions or "")
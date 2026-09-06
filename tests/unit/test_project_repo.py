"""项目级审查配置读取器测试（DESIGN 文件扩展名过滤接入 diff 管线）。

离线：临时 SQLite + project 直插，断言 `ProjectRepository.config_for` 的命中/未命中/
降级与 file_extensions 透传。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import Project
from codereview_ai.storage.project_repo import ProjectRepository


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'proj.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed(engine: AsyncEngine, **kw) -> int:
    defaults = dict(provider="github", repo_id="42", repo_full_name="a/b",
                    file_extensions="", enabled=True)
    defaults.update(kw)
    session = session_factory(engine)
    async with session() as s:
        row = Project(**defaults)
        s.add(row)
        await s.commit()
        return row.id


async def test_config_for_hit_returns_file_extensions(engine):
    await _seed(engine, provider="gitlab", repo_id="7", file_extensions=".py,.ts")
    repo = ProjectRepository(engine)
    cfg = await repo.config_for("gitlab", "7")
    assert cfg is not None and cfg.file_extensions == ".py,.ts"


async def test_config_for_missing_returns_none(engine):
    repo = ProjectRepository(engine)
    assert await repo.config_for("gitlab", "nope") is None


async def test_config_for_disabled_returns_none(engine):
    await _seed(engine, provider="github", repo_id="42", enabled=False, file_extensions=".py")
    repo = ProjectRepository(engine)
    assert await repo.config_for("github", "42") is None


async def test_config_for_default_empty_returns_empty(engine):
    await _seed(engine, provider="github", repo_id="42")  # file_extensions 缺省空
    repo = ProjectRepository(engine)
    cfg = await repo.config_for("github", "42")
    assert cfg is not None and cfg.file_extensions == ""
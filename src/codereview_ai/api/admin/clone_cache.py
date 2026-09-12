"""agent 本地克隆缓存管理 REST（后台「拉取缓存」页）。

- `GET /agent/caches`：列出缓存注册表（最近拉取信息）+ 清除策略 + pruner 运行态。
- `DELETE /agent/caches/{id}`：删单个缓存目录及其注册行（被占用 → 409）。
- `GET/PUT /agent/caches/settings`：读写清除策略（开关 + 保留天数）。
- `POST /agent/caches/prune`：立即按当前保留天数清一次超期缓存（忽略开关）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import get_db, require_permission
from codereview_ai.ops.clone_cache import (
    DEFAULT_PRUNE_DAYS,
    PRUNE_DAYS_KEY,
    PRUNE_ENABLED_KEY,
    rebuild_index,
    remove_cache_dir,
)
from codereview_ai.storage.clone_cache_repo import CloneCacheRepoRepository
from codereview_ai.storage.models import CloneCacheRepo
from codereview_ai.storage.setting_repo import SettingRepository

# 整组为管理操作（删目录/改清除策略/rebuild/prune），须 `caches:manage`，
# 不能仅登录即可访问。require_permission 已内含鉴权（登录+权限），故无需再挂 get_current_user。
router = APIRouter(prefix="/agent/caches", dependencies=[
    Depends(require_permission("caches:manage")),
])


class CacheItem(BaseModel):
    id: int
    repo_key: str
    provider: str
    repo_full_name: str
    url: str
    local_path: str
    head_sha: str
    last_error: str
    created_at: datetime
    last_fetched_at: datetime


class CacheListOut(BaseModel):
    cache_root: str
    enabled: bool
    days: int
    pruner_running: bool
    items: list[CacheItem]


class CacheSettings(BaseModel):
    enabled: bool
    days: int = Field(ge=0, le=3650)


def _to_item(row: CloneCacheRepo) -> CacheItem:
    return CacheItem(
        id=row.id, repo_key=row.repo_key, provider=row.provider,
        repo_full_name=row.repo_full_name, url=row.url, local_path=row.local_path,
        head_sha=row.head_sha, last_error=row.last_error,
        created_at=row.created_at, last_fetched_at=row.last_fetched_at,
    )


def _settings_repo(request: Request) -> SettingRepository:
    return SettingRepository(request.app.state.engine)


async def _read_settings(request: Request) -> CacheSettings:
    sr = _settings_repo(request)
    return CacheSettings(
        enabled=bool(await sr.get_bool_optional(PRUNE_ENABLED_KEY)),
        days=await sr.get_int(PRUNE_DAYS_KEY, DEFAULT_PRUNE_DAYS),
    )


@router.get("", response_model=CacheListOut)
async def list_caches(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> CacheListOut:
    rows = await CloneCacheRepoRepository(session).list_all()
    settings = await _read_settings(request)
    pruner = getattr(request.app.state, "pruner", None)
    return CacheListOut(
        cache_root=str(getattr(request.app.state, "cache_root", "")),
        enabled=settings.enabled,
        days=settings.days,
        pruner_running=bool(pruner.is_running if pruner else False),
        items=[_to_item(r) for r in rows],
    )


@router.delete("/{cache_id}", response_model=dict[str, Any])
async def delete_cache(
    cache_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    repo = CloneCacheRepoRepository(session)
    row = await repo.get(cache_id)
    if row is None:
        raise HTTPException(404, "缓存记录不存在")
    cache_root = getattr(request.app.state, "cache_root", "")
    try:
        await asyncio.to_thread(remove_cache_dir, cache_root, row.repo_key)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await repo.delete(cache_id)
    return {"id": cache_id, "repo_key": row.repo_key}


@router.get("/settings", response_model=CacheSettings)
async def get_settings(request: Request) -> CacheSettings:
    return await _read_settings(request)


@router.put("/settings", response_model=CacheSettings)
async def put_settings(body: CacheSettings, request: Request) -> CacheSettings:
    sr = _settings_repo(request)
    await sr.set(PRUNE_ENABLED_KEY, "1" if body.enabled else "0")
    await sr.set(PRUNE_DAYS_KEY, str(body.days))
    return await _read_settings(request)


@router.post("/rebuild", response_model=dict[str, Any])
async def rebuild_caches(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """扫描 cache_root 把已有的 bare 仓库补充登记进列表（仅缺失插入）。"""
    cache_root = getattr(request.app.state, "cache_root", "")
    added = await rebuild_index(CloneCacheRepoRepository(session), cache_root)
    return {"added": added, "cache_root": cache_root}


@router.post("/prune", response_model=dict[str, Any])
async def prune_caches(request: Request) -> dict[str, Any]:
    pruner = getattr(request.app.state, "pruner", None)
    if pruner is None:
        raise HTTPException(503, "清除运行器未启动")
    deleted = await pruner.force_now()
    return {"deleted": deleted, "count": len(deleted)}

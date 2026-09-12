"""`clone_cache_repo` 表存取（agentic bare-clone 缓存注册表）。

记录每个已被同步到本地 `cache_root/<slug>/` 的缓存仓库及最近拉取信息。写入由
`LocalCloneRuntime` 埋点触发（见 `sandbox.start`），列表/删除由 admin API
`api/admin/clone_cache.py` 驱动，清理由 `ops/clone_cache.py` 的 pruner 驱动。

构造传入一个已开的 `AsyncSession`（调用方确保提交/回滚归属；本仓储对写操作亦
自 commit，便于 record/prune 这类无请求事务的调用方直接使用）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.storage.models import CloneCacheRepo


class CloneCacheRepoRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_fetched(
        self,
        repo_key: str,
        *,
        provider: str,
        repo_full_name: str,
        url: str,
        local_path: str,
        head_sha: str,
    ) -> CloneCacheRepo:
        """同步成功后登记/刷新一行：存在则更新 head/最近拉取时间并清错误，否则插入。"""
        now = datetime.now(UTC)
        row = (
            await self._session.execute(
                select(CloneCacheRepo).where(CloneCacheRepo.repo_key == repo_key)
            )
        ).scalar_one_or_none()
        if row is None:
            row = CloneCacheRepo(
                repo_key=repo_key,
                provider=provider,
                repo_full_name=repo_full_name,
                url=url,
                local_path=local_path,
                head_sha=head_sha,
                last_fetched_at=now,
            )
            self._session.add(row)
        else:
            row.provider = provider
            row.repo_full_name = repo_full_name
            row.url = url
            row.local_path = local_path
            row.head_sha = head_sha
            row.last_error = ""
            row.last_fetched_at = now
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def list_all(self, limit: int = 200) -> list[CloneCacheRepo]:
        """最近拉取优先；缺省至多 200 行。"""
        rows = (
            await self._session.execute(
                select(CloneCacheRepo)
                .order_by(CloneCacheRepo.last_fetched_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def insert_if_missing(
        self,
        repo_key: str,
        *,
        local_path: str,
        last_fetched_at: datetime | None = None,
    ) -> bool:
        """仅在无该 repo_key 行时插入一条（供存量扫描建索引）；已存在返回 False 不动。

        存量目录拿不到 head/平台信息，provider/url/head_sha 留空，`repo_full_name` 落目录名
        作展示；`last_fetched_at` 默认取目录 mtime（让清除策略正确作用于存量），由调用方传入。
        """
        row = (
            await self._session.execute(
                select(CloneCacheRepo).where(CloneCacheRepo.repo_key == repo_key)
            )
        ).scalar_one_or_none()
        if row is not None:
            return False
        self._session.add(CloneCacheRepo(
            repo_key=repo_key,
            provider="",
            repo_full_name=repo_key,
            url="",
            local_path=local_path,
            head_sha="",
            last_fetched_at=last_fetched_at or datetime.now(UTC),
        ))
        await self._session.commit()
        return True

    async def list_stale(self, cutoff: datetime) -> list[CloneCacheRepo]:
        """返回最近拉取时间早于 `cutoff` 的行（供清除策略扫描）。"""
        rows = (
            await self._session.execute(
                select(CloneCacheRepo).where(CloneCacheRepo.last_fetched_at < cutoff)
            )
        ).scalars().all()
        return list(rows)

    async def get(self, cache_id: int) -> CloneCacheRepo | None:
        return (
            await self._session.execute(
                select(CloneCacheRepo).where(CloneCacheRepo.id == cache_id)
            )
        ).scalar_one_or_none()

    async def delete(self, cache_id: int) -> bool:
        """按主键删行；不存在返回 False。"""
        row = await self.get(cache_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

"""agent 本地克隆缓存清理：清除策略（DSS §12 扩展）。

agentic 审查把被审仓库 bare clone 到 `cache_root/<slug>/`。本模块负责：
- `remove_cache_dir`：删单个缓存目录（复用 `RepoCloner.remove` 的锁 + 占用判定）；
- `prune_expired`：删一批超期（最近拉取早于 cutoff）的缓存（目录 + 注册行）；
- `CloneCachePruner`：独立 asyncio 后台任务，按 `app_setting` 的清除策略
  （`agent_clone_prune_enabled` / `agent_clone_prune_days`）低频自动清理；
  `force_now()` 供「立即清理」（忽略 enabled，按 days 清超期）。

与 `schedule_job` 解耦：清除只是标量开关 + 保留天数，不需要独立调度任务。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.review.agentic.syncer import RepoCloner
from codereview_ai.storage.clone_cache_repo import CloneCacheRepoRepository
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.setting_repo import SettingRepository

logger = logging.getLogger("codereview_ai.ops.clone_cache")

#: 清除策略键（存 `app_setting`）
PRUNE_ENABLED_KEY = "agent_clone_prune_enabled"  # "1"/"0"
PRUNE_DAYS_KEY = "agent_clone_prune_days"  # 保留天数
DEFAULT_PRUNE_DAYS = 30
#: 自动清理的固定轮询间隔（低频即可）
PRUNE_INTERVAL_SECONDS = 6 * 3600


def remove_cache_dir(cache_root: str, repo_key: str) -> None:
    """删单个缓存目录；被占用（删不净）抛 `RuntimeError`，不动注册行。"""
    RepoCloner(cache_root).remove(repo_key)


def _is_repo_dir(cloner: RepoCloner, target: Path) -> bool:
    """`target` 是否为有效 git 缓存仓库（兼容两种形态）：

    - **bare 仓库**：对象库即目录自身（`RepoCloner._valid_repo`，`git rev-parse
      --is-bare-repository` = true）；
    - **普通工作树 clone**：内部有 `.git` 子目录或 `.git` gitfile 指针（老缓存/别的 agent
      机制常见，`.git` 是子目录而非对象库自身）。

    `.lock` 文件、纯残留目录、无 `.git` 的普通目录均判否，跳过不纳入。
    """
    if cloner._valid_repo(target):
        return True
    return (target / ".git").exists()


def _scan_repo_dirs(cache_root: str) -> list[str]:
    """列出 `cache_root` 下判定为有效 git 缓存仓库的子目录名（存量扫描）。

    目录名（slug）即该缓存的 repo_key。
    """
    root = Path(cache_root)
    if not root.is_dir():
        return []
    cloner = RepoCloner(cache_root)
    return [d.name for d in root.iterdir() if d.is_dir() and _is_repo_dir(cloner, d)]


def _last_activity(target: Path) -> datetime:
    """缓存的最近活动时间：普通 clone 取 `.git` 目录 mtime，否则取目录自身 mtime。"""
    git_dir = target / ".git"
    p = git_dir if git_dir.exists() else target
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)


async def rebuild_index(cache_repo: CloneCacheRepoRepository, cache_root: str) -> int:
    """把 `cache_root` 下已有的 git 缓存仓库补充登记进 `clone_cache_repo`（仅缺失时插入）。

    兼容 bare 与含 `.git` 的普通工作树 clone；`last_fetched_at` 取 `.git`/目录 mtime，
    让清除策略同样作用于存量。返回本次新增的行数；返回 0 表示全部已登记。
    """
    added = 0
    for name in _scan_repo_dirs(cache_root):
        target = Path(cache_root) / name
        if await cache_repo.insert_if_missing(name, local_path=str(target),
                                              last_fetched_at=_last_activity(target)):
            added += 1
    return added


async def prune_expired(
    cache_repo: CloneCacheRepoRepository,
    cache_root: str,
    cutoff: datetime,
) -> list[str]:
    """删除最近拉取早于 `cutoff` 的缓存（目录 + 行），返回成功删除的 key 列表。

    单个删除失败（被占用等）仅告警并跳过，不阻塞其余，避免占满磁盘时丧尸卡死。
    """
    stale = await cache_repo.list_stale(cutoff)
    deleted: list[str] = []
    for row in stale:
        try:
            await asyncio.to_thread(RepoCloner(cache_root).remove, row.repo_key)
        except Exception as exc:  # noqa: BLE001 —— 单缓存清理失败不炸整批
            logger.warning("清理缓存仓库 %s 失败：%s", row.repo_key, exc)
            continue
        ok = await cache_repo.delete(row.id)
        if ok:
            deleted.append(row.repo_key)
    return deleted


class CloneCachePruner:
    """后台按清除策略自动清理超期 agent 克隆缓存；也暴露 `force_now` 立即清理。"""

    def __init__(self, engine: AsyncEngine, cache_root: str) -> None:
        self._engine = engine
        self._cache_root = cache_root
        self._settings = SettingRepository(engine)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[object] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PRUNE_INTERVAL_SECONDS)
                return  # 置位 → 退出
            except asyncio.CancelledError:
                return
            except TimeoutError:
                pass  # 到点 → 自动清理一轮
            await self._auto_round()  # 单轮异常已隔离，不回滚整个循环

    async def _auto_round(self) -> None:
        """开启策略才清；异常隔离，不炸循环。"""
        try:
            enabled = await self._settings.get_bool_optional(PRUNE_ENABLED_KEY)
            if not enabled:
                return
            await self._force_prune()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单轮失败下轮再试
            logger.error("自动清理代理缓存失败：%s", exc, exc_info=True)

    async def force_now(self) -> list[str]:
        """立即按当前保留天数清一次（忽略 enabled），返回删除的 key 列表。"""
        return await self._force_prune()

    async def _force_prune(self) -> list[str]:
        days = await self._settings.get_int(PRUNE_DAYS_KEY, DEFAULT_PRUNE_DAYS)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        session = session_factory(self._engine)
        async with session() as s:
            deleted = await prune_expired(
                CloneCacheRepoRepository(s), self._cache_root, cutoff
            )
        if deleted:
            logger.info("清除策略（保留 %d 天）清理 %d 个缓存仓库：%s",
                        days, len(deleted), deleted)
        return deleted
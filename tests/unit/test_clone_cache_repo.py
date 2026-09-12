"""agent 克隆缓存注册表 + 清除策略测试（离线 SQLite + 临时 cache_root）。

覆盖：upsert 幂等更新、list_all/list_stale 过滤、prune_expired 删目录 + 删行。
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.ops.clone_cache import prune_expired, rebuild_index
from codereview_ai.storage.clone_cache_repo import CloneCacheRepoRepository
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import CloneCacheRepo


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def test_upsert_is_idempotent(engine: AsyncEngine) -> None:
    async with session_factory(engine)() as s:
        repo = CloneCacheRepoRepository(s)
        r1 = await repo.upsert_fetched(
            "owner_repo", provider="github", repo_full_name="owner/repo",
            url="https://x/owner/repo.git", local_path="/tmp/cache/owner_repo",
            head_sha="a" * 8,
        )
        r2 = await repo.upsert_fetched(
            "owner_repo", provider="github", repo_full_name="owner/repo",
            url="https://x/owner/repo.git", local_path="/tmp/cache/owner_repo",
            head_sha="b" * 8,
        )
    assert r1.id == r2.id  # 同一行更新而非重复插入
    async with session_factory(engine)() as s:
        rows = await CloneCacheRepoRepository(s).list_all()
        assert [r.repo_key for r in rows] == ["owner_repo"]
        assert rows[0].head_sha == "b" * 8


async def test_list_stale_filters_by_last_fetched(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with session_factory(engine)() as s:
        repo = CloneCacheRepoRepository(s)
        await repo.upsert_fetched(
            "fresh_repo", provider="github", repo_full_name="fresh/repo", url="x",
            local_path="x", head_sha="a" * 8,
        )
        s.add(CloneCacheRepo(
            repo_key="old_repo", provider="gitlab", repo_full_name="old/repo",
            url="x", local_path="x", last_fetched_at=now - timedelta(days=10),
        ))
        await s.commit()
    async with session_factory(engine)() as s:
        stale = await CloneCacheRepoRepository(s).list_stale(now - timedelta(days=5))
        keys = {r.repo_key for r in stale}
    assert "old_repo" in keys
    assert "fresh_repo" not in keys


def _mk_repo(cache_root: Path, name: str, *, bare: bool) -> Path:
    d = cache_root / name
    d.mkdir(parents=True)
    git = ["git", "init", "--bare"] if bare else ["git", "init"]
    subprocess.run([*git, str(d)], check=True, capture_output=True)
    return d


async def test_rebuild_index_scans_existing(engine: AsyncEngine, tmp_path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "not_repo").mkdir()  # 既非 bare 也无 .git → 应被忽略
    past = datetime.now(UTC) - timedelta(days=20)
    ts = past.timestamp()
    bare = _mk_repo(cache_root, "bare_repo", bare=True)
    os.utime(bare, (ts, ts))
    worktree = _mk_repo(cache_root, "worktree_repo", bare=False)  # 普通 clone（含 .git 子目录）
    os.utime(worktree / ".git", (ts, ts))
    async with session_factory(engine)() as s:
        added = await rebuild_index(CloneCacheRepoRepository(s), str(cache_root))
        assert added == 2  # 两种形态（bare + 普通 clone）都纳入
        rows = await CloneCacheRepoRepository(s).list_all()
        names = sorted(r.repo_key for r in rows)
        assert names == ["bare_repo", "worktree_repo"]  # 非仓库 not_repo 未纳入
        assert all(r.last_fetched_at.date() == past.date() for r in rows)  # 诚实反映活动时间
        added2 = await rebuild_index(CloneCacheRepoRepository(s), str(cache_root))
    assert added2 == 0  # 幂等：已登记不再重复插入


async def test_prune_expired_removes_dir_and_row(engine: AsyncEngine, tmp_path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    key = "owner_repo"
    target = cache_root / key
    target.mkdir()
    async with session_factory(engine)() as s:
        s.add(CloneCacheRepo(
            repo_key=key, provider="github", repo_full_name="owner/repo", url="x",
            local_path=str(target),
            last_fetched_at=datetime.now(UTC) - timedelta(days=10),
        ))
        await s.commit()
    async with session_factory(engine)() as s:
        deleted = await prune_expired(
            CloneCacheRepoRepository(s), str(cache_root),
            datetime.now(UTC) - timedelta(days=5),
        )
    assert deleted == [key]
    assert not target.exists()  # 目录已被物理删除
    async with session_factory(engine)() as s:
        assert await CloneCacheRepoRepository(s).list_all() == []

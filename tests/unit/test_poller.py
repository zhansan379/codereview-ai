"""ops/poller 测试：主动补拉编排（项目枚举 → 列打开 PR → 逐个审查 + 统计/异常隔离）。

`PRPoller.run_once` 的审查环节依赖 `review_pull_request`（worker 核心，已在 test_worker_pipeline
覆盖），这里把它 monkeypatch 成记录调用的 fake，专注验证 poller 的**编排**：按启用项目扫描、
幂等去重计数（new/skipped）、单 PR/单项目异常隔离不中断整轮、repo_full_name 回填。
离线：临时 SQLite + fake registry/forge，零网络。不测 `run_forever`（与 DailyReporter 同模板）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import codereview_ai.ops.poller as poller_mod
from codereview_ai.ops.poller import PRPoller
from codereview_ai.storage.db import create_engine, init_db
from codereview_ai.storage.models import Project


class FakeRegistry:
    def __init__(self, forges: dict[str, object]) -> None:
        self._forges = forges

    def get(self, provider: str) -> object | None:
        return self._forges.get(provider)

    def available(self) -> bool:
        return bool(self._forges)


class FakeForge:
    def __init__(self, prs: list | None = None, *, raise_on_list: Exception | None = None) -> None:
        self.prs = prs or []
        self.raise_on_list = raise_on_list

    async def list_open_pulls(self, repo_id: str):
        if self.raise_on_list:
            raise self.raise_on_list
        return self.prs

    async def fetch_pull_request(self, pr):
        return pr


def _pr(number: int, head: str, *, full_name: str = "") -> object:
    """构造一个足够像 PullRequest 的对象（poller 只改/读 repo_full_name）。"""
    class _PR:
        pass
    p = _PR()
    p.pr_number = number
    p.head_sha = head
    p.repo_full_name = full_name
    p.provider = "github"
    p.repo_id = f"acme/widgets"
    return p


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'poller.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed_project(engine: AsyncEngine, *, provider="github", repo_id="acme/widgets",
                        repo_full_name="acme/widgets") -> None:
    from sqlalchemy import select
    from codereview_ai.storage.db import session_factory
    session = session_factory(engine)
    async with session() as s:
        existing = (await s.execute(select(Project).limit(1))).scalar_one_or_none()
        if existing is None:
            s.add(Project(provider=provider, repo_id=repo_id,
                          repo_full_name=repo_full_name, enabled=True))
            await s.commit()


async def test_run_once_scans_and_counts_new_vs_skipped(engine, tmp_path, monkeypatch):
    await _seed_project(engine)
    pr_new = _pr(101, "head-new")  # repo_full_name 空 → 用项目回填
    pr_old = _pr(102, "head-old")

    calls: list[tuple[object, object, object]] = []

    async def fake_review(forge, reviewer, pr, **kwargs):
        calls.append((forge, reviewer, pr))
        return "reviewed" if pr.pr_number == 101 else "already"

    monkeypatch.setattr(poller_mod, "review_pull_request", fake_review)
    registry = FakeRegistry({"github": FakeForge([pr_new, pr_old])})
    poller = PRPoller(engine, registry, reviewer=None)  # type: ignore[arg-type]

    report = await poller.run_once()
    assert report["projects"] == 1
    assert report["prs"] == 2
    assert report["new"] == 1
    assert report["skipped"] == 1
    assert report["errors"] == []
    assert len(calls) == 2
    # 补拉项缺 repo_full_name 时用项目行回填
    assert pr_new.repo_full_name == "acme/widgets"
    assert pr_old.repo_full_name == "acme/widgets"


async def test_run_once_isolates_single_pr_error(engine, monkeypatch):
    await _seed_project(engine)
    prs = [_pr(101, "head-a"), _pr(102, "head-b")]

    async def fake_review(forge, reviewer, pr, **kwargs):
        if pr.pr_number == 101:
            raise RuntimeError("boom")
        return "reviewed"

    monkeypatch.setattr(poller_mod, "review_pull_request", fake_review)
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge(prs)}), reviewer=None)  # type: ignore[arg-type]
    report = await poller.run_once()
    assert report["new"] == 1  # 101 失败隔离，102 仍完成
    assert len(report["errors"]) == 1
    assert "boom" in report["errors"][0]


async def test_run_once_skips_project_without_adapter(engine, monkeypatch):
    await _seed_project(engine)  # github 项目，但 registry 只有 gitlab
    monkeypatch.setattr(poller_mod, "review_pull_request",
                        lambda forge, reviewer, pr, **kw: asyncio.sleep(0) or "reviewed")
    poller = PRPoller(engine, FakeRegistry({"gitlab": FakeForge([])}), reviewer=None)  # type: ignore[arg-type]
    report = await poller.run_once()
    assert report["projects"] == 0  # 适配器缺失项目不计入扫描数、不报错
    assert report["errors"] == []


async def test_run_once_isolates_list_error(engine):
    await _seed_project(engine)
    forge = FakeForge(raise_on_list=RuntimeError("net down"))
    poller = PRPoller(engine, FakeRegistry({"github": forge}), reviewer=None)  # type: ignore[arg-type]
    report = await poller.run_once()
    assert report["projects"] == 1
    assert report["prs"] == 0
    assert len(report["errors"]) == 1
    assert "列打开 PR 失败" in report["errors"][0]
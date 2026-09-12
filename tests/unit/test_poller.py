"""ops/poller 测试：主动补拉编排（项目枚举 → 列打开 PR → 跳过已审 + 落 queued 行 + 入队）。

补拉**不再内联跑审查**：只发现打开 PR → 给每个未审过 PR 建 `queued` 审计行 → 入队到 worker
队列异步审查立即返回。这里把 `enqueue_pr` monkeypatch/fake 成记录调用的替身事件核，专验编排：
按启用项目扫描、幂等去重（new/skipped）、单 PR/单项目异常隔离不中断整轮、repo_full_name 回填、
`_run_lock` 互斥。审查真正的执行路径（enqueue_pr → worker → review_pull_request）在
test_worker_pipeline 的集成用例覆盖。离线：临时 SQLite + fake registry/forge，零网络。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.domain.models import PullRequest
from codereview_ai.ops.poller import PRPoller
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import AppSetting, Project, ReviewTask


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
        self.list_calls: list[tuple[str, bool]] = []  # (repo_id, include_closed)

    async def list_pulls(self, repo_id: str, *, include_closed: bool = False):
        self.list_calls.append((repo_id, include_closed))
        if self.raise_on_list:
            raise self.raise_on_list
        return self.prs

    async def fetch_pull_request(self, pr):
        return pr


class FakeEnqueuer:
    """替身事件核：只记录入队调用，或按需阻塞/抛错，验证 poller 的编排而非审查本身。"""

    def __init__(
        self, *, raise_on: set[int] | None = None, hold: asyncio.Event | None = None
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.raise_on = raise_on or set()
        self.hold = hold

    async def enqueue_pr(self, provider: str, pr):
        if self.hold:
            await self.hold.wait()
        if pr.pr_number in self.raise_on:
            raise RuntimeError("boom")
        self.calls.append((provider, pr))
        return f"task-{pr.pr_number}"


def _pr(number: int, head: str, *, full_name: str = "") -> PullRequest:
    """构造真实的 frozen PullRequest（poller 只读字段；缺 full_name 由项目行 replace 回填）。"""
    return PullRequest(
        provider="github", repo_id="acme/widgets", repo_full_name=full_name,
        web_url=f"https://example/{number}", pr_number=number, title=f"PR #{number}",
        source_branch="feature/x", target_branch="main", head_sha=head,
        base_sha="abcdef0", author="alice",
    )


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'poller.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed_project(engine: AsyncEngine, *, provider="github", repo_id="acme/widgets",
                        repo_full_name="acme/widgets") -> None:
    session = session_factory(engine)
    async with session() as s:
        existing = (await s.execute(select(Project).limit(1))).scalar_one_or_none()
        if existing is None:
            s.add(Project(provider=provider, repo_id=repo_id,
                          repo_full_name=repo_full_name, enabled=True))
            await s.commit()


async def _seed_completed_review(engine: AsyncEngine, *, provider="github", repo_id="acme/widgets",
                                 pr_number: int, head_sha: str) -> None:
    """落一条 completed 的 mr 审查行，使 same-head 的补拉项被判为已审跳过。"""
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider=provider, repo_id=repo_id, pr_number=pr_number,
                         event_type="mr", branch="feature/x", head_sha=head_sha,
                         base_sha="abcdef0", pr_title=f"PR #{pr_number}",
                         web_url=f"https://example/{pr_number}", state="completed"))
        await s.commit()


async def test_run_once_scans_and_enqueues_new(engine, tmp_path):
    await _seed_project(engine)
    pr_new = _pr(101, "head-new")  # repo_full_name 空 → 用项目回填
    pr_old = _pr(102, "head-old")

    enqueuer = FakeEnqueuer()
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge([pr_new, pr_old])}), enqueuer)

    report = await poller.run_once()
    assert report["projects"] == 1
    assert report["prs"] == 2
    assert report["new"] == 2  # 无已审记录 → 全部入队待审
    assert report["skipped"] == 0
    assert report["errors"] == []
    assert [c[1].pr_number for c in enqueuer.calls] == [101, 102]
    # 补拉项缺 repo_full_name 时用项目行回填：frozen dataclass，入队的是 replace 后的新实例
    assert [c[1].repo_full_name for c in enqueuer.calls] == ["acme/widgets", "acme/widgets"]


async def _seed_skipped_review(engine: AsyncEngine, *, provider="github", repo_id="acme/widgets",
                               pr_number: int, head_sha: str) -> None:
    """落一条 skipped(mr_disabled) 的 mr 审查行：已存储、已下过判断，补拉应视为已处理跳过。"""
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider=provider, repo_id=repo_id, pr_number=pr_number,
                         event_type="mr", branch="feature/x", head_sha=head_sha,
                         base_sha="abcdef0", pr_title=f"PR #{pr_number}",
                         web_url=f"https://example/{pr_number}", state="skipped",
                         skip_reason="mr_disabled"))
        await s.commit()


async def test_run_once_skips_same_head_skipped_already_stored(engine, tmp_path):
    """存量 skipped(mr_disabled) 行不该当成「新入队」：同 head 记为 skipped、不入队。"""
    await _seed_project(engine)
    await _seed_skipped_review(engine, pr_number=101, head_sha="head-skip")
    pr_skipped = _pr(101, "head-skip")
    pr_new = _pr(102, "head-new")

    enqueuer = FakeEnqueuer()
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge([pr_skipped, pr_new])}), enqueuer)

    report = await poller.run_once()
    assert report["new"] == 1          # 只有全新 head 的 102 入队
    assert report["skipped"] == 1      # 已存储的 101 记为已审过跳过
    assert report["errors"] == []
    assert [c[1].pr_number for c in enqueuer.calls] == [102]


async def test_run_once_reenqueues_failed_head(engine, tmp_path):
    """failed 头从未成功审过 → 补拉重新入队作重试（可再被审）。"""
    await _seed_project(engine)
    session = session_factory(engine)
    async with session() as s:
        s.add(ReviewTask(provider="github", repo_id="acme/widgets", pr_number=101,
                         event_type="mr", branch="feature/x", head_sha="head-fail",
                         base_sha="abcdef0", pr_title="PR #101",
                         web_url="https://example/101", state="failed", error="boom"))
        await s.commit()
    pr_fail = _pr(101, "head-fail")

    enqueuer = FakeEnqueuer()
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge([pr_fail])}), enqueuer)

    report = await poller.run_once()
    assert report["new"] == 1
    assert report["skipped"] == 0
    assert [c[1].pr_number for c in enqueuer.calls] == [101]


async def test_run_once_skips_same_head_already_reviewed(engine, tmp_path):
    """同 head 已 completed（last_ok_review 命中）→ 记为 skipped、不入队。"""
    await _seed_project(engine)
    await _seed_completed_review(engine, pr_number=101, head_sha="head-done")
    pr_done = _pr(101, "head-done")
    pr_new = _pr(102, "head-new")

    enqueuer = FakeEnqueuer()
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge([pr_done, pr_new])}), enqueuer)

    report = await poller.run_once()
    assert report["new"] == 1
    assert report["skipped"] == 1
    assert report["errors"] == []
    assert [c[1].pr_number for c in enqueuer.calls] == [102]  # 已审的 101 未入队
    assert poller.progress == {"done": 2, "total": 2, "new": 1, "skipped": 1}


async def test_run_once_isolates_single_pr_error(engine, monkeypatch):
    await _seed_project(engine)
    prs = [_pr(101, "head-a"), _pr(102, "head-b")]

    enqueuer = FakeEnqueuer(raise_on={101})
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge(prs)}), enqueuer)
    report = await poller.run_once()
    assert report["new"] == 1  # 101 入队失败隔离，102 仍完成
    assert len(report["errors"]) == 1
    assert "boom" in report["errors"][0]
    assert [c[1].pr_number for c in enqueuer.calls] == [102]


async def test_run_once_skips_project_without_adapter(engine):
    await _seed_project(engine)  # github 项目，但 registry 只有 gitlab
    poller = PRPoller(engine, FakeRegistry({"gitlab": FakeForge([])}), FakeEnqueuer())
    report = await poller.run_once()
    assert report["projects"] == 0  # 适配器缺失项目不计入扫描数、不报错
    assert report["errors"] == []


async def test_run_once_isolates_list_error(engine):
    await _seed_project(engine)
    forge = FakeForge(raise_on_list=RuntimeError("net down"))
    poller = PRPoller(engine, FakeRegistry({"github": forge}), FakeEnqueuer())
    report = await poller.run_once()
    assert report["projects"] == 1
    assert report["prs"] == 0
    assert len(report["errors"]) == 1
    assert "列 PR 失败" in report["errors"][0]


async def test_run_once_conflicts_when_another_running(engine):
    """第二轮补拉撞上在跑的一轮 → 返回 conflict，不等待不重叠（手动/定时共用闸）。"""
    await _seed_project(engine)
    hold = asyncio.Event()
    enqueuer = FakeEnqueuer(hold=hold)
    poller = PRPoller(engine, FakeRegistry({"github": FakeForge([_pr(101, "h1")])}), enqueuer)

    t1 = asyncio.create_task(poller.run_once())
    for _ in range(20):  # 轮流让出，等 t1 拿到锁并停在入队 await
        await asyncio.sleep(0)
        if poller._run_lock.locked():
            break
    assert poller._run_lock.locked(), "第一轮应已持锁（入队在挂起）"

    t2 = asyncio.create_task(poller.run_once())
    res2 = await t2
    assert res2.get("conflict") is True
    assert res2["new"] == 0 and res2["skipped"] == 0 and res2["errors"] == []

    hold.set()
    res1 = await t1
    assert res1["new"] == 1
    # 逐条进度已累计（含 total/done），供 /status 展示
    assert poller.progress == {"done": 1, "total": 1, "new": 1, "skipped": 0}


async def test_run_once_include_closed_default_off(engine):
    """无落库行时回落 env 默认（False）→ 调适配器 include_closed=False。"""
    await _seed_project(engine)
    forge = FakeForge([_pr(101, "h1")])
    poller = PRPoller(engine, FakeRegistry({"github": forge}), FakeEnqueuer())
    await poller.run_once()
    assert forge.list_calls == [("acme/widgets", False)]


async def test_run_once_include_closed_from_db(engine):
    """落库 `poll_include_closed=1` → 调适配器 include_closed=True（含已关闭 PR）。"""
    await _seed_project(engine)
    session = session_factory(engine)
    async with session() as s:
        s.add(AppSetting(key="poll_include_closed", value="1"))
        await s.commit()
    forge = FakeForge([_pr(101, "h1")])
    poller = PRPoller(engine, FakeRegistry({"github": forge}), FakeEnqueuer())
    await poller.run_once()
    assert forge.list_calls == [("acme/widgets", True)]


async def test_run_once_include_closed_env_default_true(engine):
    """无落库行且 env 默认 True → 回落到 include_closed=True。"""
    await _seed_project(engine)
    forge = FakeForge([_pr(101, "h1")])
    poller = PRPoller(
        engine, FakeRegistry({"github": forge}), FakeEnqueuer(),
        include_closed_default=True,
    )
    await poller.run_once()
    assert forge.list_calls == [("acme/widgets", True)]

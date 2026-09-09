"""离线单测：agentic 对话采集（contextvar 注入 + 逐条落库）。

- 开关注入：`conversation_capture` 作用域内 `ACTIVE_RECORDER` 生效、退出还原；无 recorder
  时 `None`。adapter 侧只读 `ACTIVE_RECORDER.get()`，故这里断言「注入后读得到」。
- 阶段标记：默认 `"loop"`；`set_phase` 设/还原，不进作用域时后台压缩轮落 loop。
- 落库：临时 SQLite 上 recorder 逐条写 `review_conversation`，seq 单调递增、字段入座，
  且 `task_id=None` 时不写（不采集路径）。
- 护栏：写入失败（坏 engine/加塞提交异常）**不抛、不阻断**审查主链。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.review.agentic.capture import (
    ACTIVE_PHASE,
    ACTIVE_RECORDER,
    ConversationRecorder,
    conversation_capture,
    diff_usage_sink,
    set_phase,
)
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ModelUsage, ReviewConversation, ReviewTask


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'conv.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed_task(engine: AsyncEngine) -> int:
    """插一条父级 review_task 供 FK（recorder 生产时 `ensure_task` 已建行）。"""
    session = session_factory(engine)
    async with session() as s:
        t = ReviewTask(provider="gitlab", repo_id="9", pr_number=42, event_type="mr",
                       branch="main", head_sha="h", state="completed", score_total=0)
        s.add(t)
        await s.commit()
        return t.id


# ── contextvar 开关注入 ─────────────────────────────────────────────────


async def test_conversation_capture_sets_and_restores_recorder():
    recorder = ConversationRecorder(object(), task_id=1)  # type: ignore[arg-type]
    assert ACTIVE_RECORDER.get() is None
    async with conversation_capture(recorder):
        assert ACTIVE_RECORDER.get() is recorder  # adapter 侧 `get()` 读得到采集器
    assert ACTIVE_RECORDER.get() is None  # 退出还原
    # None 采集器 → 不 set（幂等、无副作用）
    async with conversation_capture(None):
        assert ACTIVE_RECORDER.get() is None


async def test_set_phase_default_and_reset():
    assert ACTIVE_PHASE.get() == "loop"  # 未 set 的后台压缩轮默认 loop
    restore = set_phase("plan")
    assert ACTIVE_PHASE.get() == "plan"
    restore()
    assert ACTIVE_PHASE.get() == "loop"


# ── 逐条落库 ───────────────────────────────────────────────────────────


async def test_record_writes_row_and_seq_monotonic(engine: AsyncEngine):
    task_id = await _seed_task(engine)
    rec = ConversationRecorder(engine, task_id=task_id, trace_id="trace-abc")
    await rec.record("main", request=[{"role": "user", "content": "hello"}],
                     response={"content": "hi", "tool_calls": [], "usage": {"total_tokens": 9}},
                     model="claude-m")
    # 第三条带 2 个 tool_calls → 计入 metrics().tool_calls（worker 收尾同源采集）
    await rec.record("scoring", request=[{"role": "user", "content": "x"}],
                     response={"content": '{"ok":1}', "tool_calls": [{"id": "a"}, {"id": "b"}]},
                     model="claude-m")

    session = session_factory(engine)
    async with session() as s:
        rows = (await s.execute(
            select(ReviewConversation).where(ReviewConversation.task_id == task_id)
            .order_by(ReviewConversation.id)
        )).scalars().all()

    assert [r.seq for r in rows] == [1, 2]  # 单调递增
    assert [r.phase for r in rows] == ["main", "scoring"]
    assert rows[0].model == "claude-m" and rows[0].trace_id == "trace-abc"
    assert json.loads(rows[0].request_json)[0]["content"] == "hello"
    assert json.loads(rows[0].response_json)["content"] == "hi"
    # 内存累计指标：轮数=条数，工具调用数只数带 tool_calls 的那条（2 个）
    assert rec.metrics() == (2, 2)

    # 同步写用量行：第一条带 usage（9 token）落 model_usage；第二条无 usage 不落
    session = session_factory(engine)
    async with session() as s:
        usage_rows = (await s.execute(
            select(ModelUsage).where(ModelUsage.task_id == task_id)
        )).scalars().all()
    assert len(usage_rows) == 1
    assert usage_rows[0].model == "claude-m"
    assert usage_rows[0].total_tokens == 9
    assert usage_rows[0].phase == "main"


async def test_record_noop_when_task_id_none(engine: AsyncEngine):
    rec = ConversationRecorder(engine, task_id=None)
    await rec.record("loop", request=[], response={})  # 不落库也不抛
    session = session_factory(engine)
    async with session() as s:
        n = (await s.execute(select(ReviewConversation))).scalars().all()
    assert n == []


async def test_diff_usage_sink_writes_model_usage(engine: AsyncEngine):
    """diff 模式补记：gateway 回调 sink → 落 model_usage（看板 Token/成本按模式拆分）。"""
    task_id = await _seed_task(engine)
    sink = diff_usage_sink(engine, task_id)
    await sink({
        "model": "deepseek/deepseek-v4-flash",
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "total_tokens": 125,
    })
    await sink({"model": "", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})  # 空值不抛

    session = session_factory(engine)
    async with session() as s:
        rows = (await s.execute(
            select(ModelUsage).where(ModelUsage.task_id == task_id).order_by(ModelUsage.id)
        )).scalars().all()
    assert len(rows) == 2
    r = rows[0]
    assert r.model == "deepseek/deepseek-v4-flash"
    assert (r.prompt_tokens, r.completion_tokens, r.total_tokens) == (100, 25, 125)
    assert r.cost >= 0  # 定价表命中则 >0，否则一致降级为 0，均不阻断
    assert rows[1].total_tokens == 0


# ── 护栏：写入异常不阻断 ────────────────────────────────────────────────


class _BoomSession:
    """commit 即炸的假会话：验证对话落库失败被吞掉，不向审查链抛。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, *args):  # noqa: ANN002
        return None

    async def commit(self):
        raise RuntimeError("db down")

    async def rollback(self):
        return None


class _Boom:
    def __call__(self):
        return _BoomSession()


async def test_record_swallows_write_failure(monkeypatch):
    monkeypatch.setattr(
        "codereview_ai.review.agentic.capture.session_factory",
        lambda _engine: _Boom(),  # `session()` 返回 _BoomSession
    )
    rec = ConversationRecorder(object(), task_id=7)  # type: ignore[arg-type]
    # 不抛异常（护栏），审查主链照常继续
    await rec.record("main", request=[{"role": "user", "content": "x"}], response={})


# ── adapter 侧读取（模拟 ToolCallingLLM.chat 探测）─────────────────────


async def test_adapter_detects_active_recorder_via_contextvar(engine: AsyncEngine):
    """llm_adapter 的采集点只读 `ACTIVE_RECORDER.get()`；作用域内就该拉得到。

    用真 recorder + conversation_capture 模拟 worker 开审包裹，确认非 None 时能拿到并落库，
    即 adapter 的 `if rec := ACTIVE_RECORDER.get(): await rec.record(...)` 在真链路可达。
    """
    task_id = await _seed_task(engine)
    rec = ConversationRecorder(engine, task_id=task_id, trace_id="t")
    async with conversation_capture(rec):
        found = ACTIVE_RECORDER.get()
        assert found is not None
        await found.record("plan", request=[], response={"content": "p"}, model="m")
    session = session_factory(engine)
    async with session() as s:
        rows = (await s.execute(
            select(ReviewConversation).where(ReviewConversation.task_id == task_id)
        )).scalars().all()
    assert len(rows) == 1 and rows[0].phase == "plan"

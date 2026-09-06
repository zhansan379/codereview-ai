"""DB 驱动配置仓库测试（DESIGN §16）：分层合并、TTL 缓存、白名单、env 重放、离线审查装配。

离线：临时 SQLite + 注入 fake LLM backend（零网络、零 token）。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.config.repository import (
    CACHE_TTL_SECONDS,
    FORBIDDEN_OVERRIDE_WORDS,
    ConfigRepository,
    is_overrideable,
    validate_override_key,
)
from codereview_ai.crypto import encrypt
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import ModelConfig, NotifierConfig


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'repo.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed_model(engine: AsyncEngine, **kw) -> int:
    defaults = dict(name="m", provider="deepseek", model="deepseek-chat",
                    api_key_encrypted="", priority=0, enabled=True, base_url="")
    defaults.update(kw)
    session = session_factory(engine)
    async with session() as s:
        row = ModelConfig(**defaults)
        s.add(row)
        await s.commit()
        return row.id



async def test_resolve_llm_picks_highest_priority(engine):
    key = _fernet_key()
    await _seed_model(engine, name="low", model="m-low", priority=1,
                      api_key_encrypted=encrypt("k-low", key), enabled=True)
    await _seed_model(engine, name="high", model="m-high", priority=9,
                      api_key_encrypted=encrypt("k-high", key), enabled=True)

    repo = ConfigRepository(engine, encryption_key=key)
    llm = await repo.resolve_llm()
    assert llm is not None
    assert llm.model == "m-high"
    assert llm.api_key == "k-high"
    assert llm.env["deepseek_api_key"] == "k-high"



async def test_disabled_model_not_selected(engine):
    key = _fernet_key()
    await _seed_model(engine, name="off", model="m-off", priority=99, enabled=False,
                      api_key_encrypted=encrypt("k", key))
    repo = ConfigRepository(engine, encryption_key=key)
    assert await repo.resolve_llm() is None



async def test_notifier_routes_filter_by_project_and_decrypt(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="m", model="m")
    session = session_factory(engine)
    async with session() as s:
        s.add(NotifierConfig(channel="dingtalk", enabled=True,
                             webhook_encrypted=encrypt("https://w-global", key),
                             secret_encrypted=encrypt("SEC-1", key),
                             project_id=None, at_threshold=60))
        s.add(NotifierConfig(channel="feishu", enabled=True,
                             webhook_encrypted=encrypt("https://w-proj", key),
                             secret_encrypted="", project_id=7, at_threshold=90))
        s.add(NotifierConfig(channel="wecom", enabled=False,
                             webhook_encrypted=encrypt("https://w-off", key),
                             secret_encrypted="", project_id=None, at_threshold=60))
        await s.commit()

    repo = ConfigRepository(engine, encryption_key=key)
    global_only = await repo.notifier_routes(project_id=None)
    assert [r.channel for r in global_only] == ["dingtalk"]
    proj = await repo.notifier_routes(project_id=7)
    assert {r.channel for r in proj} == {"dingtalk", "feishu"}
    ding = next(r for r in proj if r.channel == "dingtalk")
    assert ding.webhook == "https://w-global" and ding.secret == "SEC-1"



async def test_ttl_cache_hit_and_expiry(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="a", model="m-a", priority=5)
    repo = ConfigRepository(engine, encryption_key=key)

    llm = await repo.resolve_llm()
    assert llm and llm.model == "m-a"

    # TTL 内存缓存命中：DB 已删模型但仍取到缓存（同一次构造、未过期）
    session = session_factory(engine)
    async with session() as s:
        for row in (await s.execute(select(ModelConfig))).scalars().all():
            await s.delete(row)
        await s.commit()
    assert (await repo.resolve_llm()).model == "m-a"  # 未过期 → 命中缓存

    # 手动把缓存标记为陈旧，模拟 TTL 到期 → 重新拉取后模型为 None
    repo._loaded_at = datetime.now(UTC) - timedelta(seconds=CACHE_TTL_SECONDS + 1)
    stub_clock = iter([datetime.now(UTC)])

    async def _fake_fetch() -> None:
        repo._models = []
        repo._notifiers = []
        repo._loaded_at = next(stub_clock)

    monkeypatch.setattr(repo, "_fetch", _fake_fetch)
    assert await repo.resolve_llm() is None  # TTL 到期 → 重新拉取（fake 清空）



async def test_transient_failure_not_cached_stale_returned(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="a", model="m-a", priority=5)
    repo = ConfigRepository(engine, encryption_key=key)
    assert (await repo.resolve_llm()).model == "m-a"

    calls = {"n": 0}

    async def _flaky() -> None:
        calls["n"] += 1
        raise RuntimeError("db down")

    # 把缓存标记为陈旧，迫使下一次调用重新拉取
    repo._loaded_at = datetime.now(UTC) - timedelta(seconds=CACHE_TTL_SECONDS + 1)
    monkeypatch.setattr(repo, "_fetch", _flaky)
    # DB 暂不可用：返回上次缓存；TTL 时间戳未刷新（下次会再试）
    assert (await repo.resolve_llm()).model == "m-a"
    assert calls["n"] == 1
    # 再调用一次仍会重试（失败未缓存，时间戳未刷新）
    assert (await repo.resolve_llm()).model == "m-a"
    assert calls["n"] == 2



async def test_whitelist_blocks_forbidden_and_unknown(engine):
    # 禁词一票否决：api_key / secret / token 全都不可覆盖
    for bad in ("model.api_key", "notifier.webhook_secret", "auth.token"):
        assert not is_overrideable(bad)
        with pytest.raises(ValueError):
            validate_override_key(bad)
    # 白名单内键可覆盖；白名单外普通键拒绝
    assert is_overrideable("review.style")
    assert not is_overrideable("logging.level")  # 不在白名单分节
    validate_override_key("review.style")
    with pytest.raises(ValueError):
        validate_override_key("logging.level")


def test_comment_whitelist_not_ect():
    assert FORBIDDEN_OVERRIDE_WORDS



async def test_env_replay_never_overrides_host(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="m", provider="deepseek", model="m-db",
                      api_key_encrypted=encrypt("k-db", key))
    monkeypatch.setenv("deepseek_api_key", "k-host")  # host 显式配置

    repo = ConfigRepository(engine, encryption_key=key)
    llm = await repo.resolve_llm()
    applied = repo.apply_env_replay(llm)
    assert applied == []  # host 已设键不被 DB 覆盖
    assert __import__("os").environ["deepseek_api_key"] == "k-host"



async def test_env_model_overrides_db(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="db-model", model="m-db", priority=5)
    monkeypatch.setenv("CR_LLM_MODEL", "m-env-haiku")

    repo = ConfigRepository(engine, encryption_key=key)
    llm = await repo.resolve_llm()
    assert llm is not None and llm.model == "m-env-haiku"  # env 重放压 DB



async def test_build_reviewer_uses_db_model(engine, monkeypatch):
    key = _fernet_key()
    calls: list[str] = []
    await _seed_model(engine, name="db-model", model="m-db", priority=5,
                      api_key_encrypted=encrypt("k-db", key))

    async def fake_backend(messages):
        calls.append("called")
        return "ok"

    repo = ConfigRepository(engine, encryption_key=key)
    reviewer = await repo.build_reviewer(backend=fake_backend)
    assert reviewer is not None
    assert isinstance(reviewer, Reviewer)
    assert calls == []  # follower：评审时才发请求


async def test_build_reviewer_threads_max_tokens_and_temperature(engine):
    key = _fernet_key()
    await _seed_model(engine, name="db-model", model="m-db", priority=5,
                      max_tokens=4096, temperature=0.3)

    async def fake_backend(messages):
        return "ok"

    repo = ConfigRepository(engine, encryption_key=key)
    reviewer = await repo.build_reviewer(backend=fake_backend)
    gateway = reviewer.gateway
    assert gateway.max_tokens == 4096  # DB max_tokens 真正透传给 litellm（治坏 JSON 截断）
    assert gateway.temperature == 0.3



async def test_apply_env_replay_fills_missing(engine, monkeypatch):
    key = _fernet_key()
    await _seed_model(engine, name="m", provider="deepseek", model="m-db",
                      api_key_encrypted=encrypt("k-db", key))
    monkeypatch.delenv("deepseek_api_key", raising=False)

    repo = ConfigRepository(engine, encryption_key=key)
    llm = await repo.resolve_llm()
    applied = repo.apply_env_replay(llm)
    assert applied == ["deepseek_api_key"]
    assert __import__("os").environ["deepseek_api_key"] == "k-db"

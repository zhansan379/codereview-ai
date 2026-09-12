"""DB 驱动配置仓库测试（DESIGN §16）：分层合并、白名单、env 重放、离线审查装配。

离线：临时 SQLite + 注入 fake LLM backend（零网络、零 token）。
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.config.repository import (
    DEFAULT_FORGE_URLS,
    FORBIDDEN_OVERRIDE_WORDS,
    ConfigRepository,
    is_overrideable,
    validate_override_key,
)
from codereview_ai.crypto import encrypt
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import (
    ForgeConfig,
    ModelConfig,
    NotifierConfig,
    NotifierMember,
    NotifierRouteMember,
)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(b"\x00" * 32).decode()


@pytest.fixture
async def engine(tmp_path) -> AsyncEngine:
    url = f"sqlite+aiosqlite:///{tmp_path / 'repo.db'}"
    eng = create_engine(url)
    await init_db(eng)
    yield eng
    await eng.dispose()


async def _seed_forge(engine: AsyncEngine, **kw) -> int:
    defaults = dict(provider="github", url="", token_encrypted="", enabled=True)
    defaults.update(kw)
    session = session_factory(engine)
    async with session() as s:
        row = ForgeConfig(**defaults)
        s.add(row)
        await s.commit()
        return row.id


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
        await s.flush()
        # 系统级成员 + 渠道绑定（@指定成员）
        alice = NotifierMember(name="Alice", git_username="alice", dingtalk_mobile="13800000000")
        bob = NotifierMember(name="Bob", git_username="bob", wecom_userid="wbob")
        s.add_all([alice, bob])
        await s.flush()
        ding = (
            await s.scalars(select(NotifierConfig).where(NotifierConfig.channel == "dingtalk"))
        ).one()
        s.add(NotifierRouteMember(notifier_id=ding.id, member_id=alice.id))
        s.add(NotifierRouteMember(notifier_id=ding.id, member_id=bob.id))
        await s.commit()

    repo = ConfigRepository(engine, encryption_key=key)
    global_only = await repo.notifier_routes(project_id=None)
    assert [r.channel for r in global_only] == ["dingtalk"]
    proj = await repo.notifier_routes(project_id=7)
    assert {r.channel for r in proj} == {"dingtalk", "feishu"}
    ding = next(r for r in proj if r.channel == "dingtalk")
    assert ding.webhook == "https://w-global" and ding.secret == "SEC-1"
    # @ 指定成员装载：渠道绑定的系统级成员进入路由（git_username 作解析键）
    assert [m.name for m in ding.at_members] == ["Alice", "Bob"]
    feishu = next(r for r in proj if r.channel == "feishu")
    assert feishu.at_members == []

    # 提交者按 git_username 命中成员
    resolved = await repo.resolve_member_by_git_username("bob")
    assert resolved is not None and resolved.wecom_userid == "wbob"
    assert await repo.resolve_member_by_git_username("nobody") is None






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


async def test_resolve_llm_chain_priority_desc_with_decrypt(engine):
    key = _fernet_key()
    await _seed_model(engine, name="low", model="m-low", priority=1,
                      api_key_encrypted=encrypt("k-low", key))
    await _seed_model(engine, name="high", model="m-high", priority=9,
                      api_key_encrypted=encrypt("k-high", key))

    repo = ConfigRepository(engine, encryption_key=key)
    chain = await repo.resolve_llm_chain()
    assert [m.model for m in chain] == ["m-high", "m-low"]  # priority 降序
    assert [m.api_key for m in chain] == ["k-high", "k-low"]


async def test_resolve_llm_chain_excludes_disabled(engine):
    key = _fernet_key()
    await _seed_model(engine, name="enabled", model="m-on", priority=5,
                      api_key_encrypted=encrypt("k", key))
    await _seed_model(engine, name="disabled", model="m-off", priority=99, enabled=False)
    repo = ConfigRepository(engine, encryption_key=key)
    assert [m.model for m in await repo.resolve_llm_chain()] == ["m-on"]


async def test_resolve_llm_chain_env_model_overrides(engine, monkeypatch):
    await _seed_model(engine, name="db", model="m-db", priority=5)
    monkeypatch.setenv("CR_LLM_MODEL", "m-env")
    repo = ConfigRepository(engine, encryption_key=_fernet_key())
    chain = await repo.resolve_llm_chain()
    assert [m.model for m in chain] == ["m-env"]


async def test_build_reviewer_wraps_multiple_models_in_fallback(engine):
    from codereview_ai.review.fallback import FallbackLLMGateway

    key = _fernet_key()
    await _seed_model(engine, name="low", model="m-low", priority=1,
                      api_key_encrypted=encrypt("k-low", key))
    await _seed_model(engine, name="high", model="m-high", priority=9,
                      api_key_encrypted=encrypt("k-high", key))

    async def fake_backend(messages):
        return "ok"

    repo = ConfigRepository(engine, encryption_key=key)
    reviewer = await repo.build_reviewer(backend=fake_backend)
    assert reviewer is not None
    fb = reviewer.gateway
    assert isinstance(fb, FallbackLLMGateway)
    assert fb.model == "m-high"  # 链首=主模型（高 priority）


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


# ---------- resolve_forge（平台 token/url，env 优先、DB 兜底） ----------


async def test_resolve_forge_env_overrides_db_with_default_url(engine, monkeypatch):
    key = _fernet_key()
    await _seed_forge(engine, provider="github", url="https://gh-enterprise.example",
                      token_encrypted=encrypt("k-db", key))
    monkeypatch.setenv("CR_GITHUB_TOKEN", "k-env")  # 只设 env token，未设 URL
    repo = ConfigRepository(engine, encryption_key=key)
    res = await repo.resolve_forge("github")
    assert res is not None and res.source == "env"
    assert res.token == "k-env"
    assert res.url == DEFAULT_FORGE_URLS["github"]  # env 无 URL → 默认兜底


async def test_resolve_forge_env_url_used(engine, monkeypatch):
    repo = ConfigRepository(engine, encryption_key=_fernet_key())
    monkeypatch.setenv("CR_GITLAB_URL", "https://gl.selfhost.example")
    monkeypatch.setenv("CR_GITLAB_TOKEN", "t")
    res = await repo.resolve_forge("gitlab")
    assert res is not None and res.url == "https://gl.selfhost.example" and res.token == "t"


async def test_resolve_forge_db_fallback_decrypt(engine, monkeypatch):
    key = _fernet_key()
    await _seed_forge(engine, provider="gitlab", url="https://gl.selfhost.example",
                      token_encrypted=encrypt("k-db", key))
    monkeypatch.delenv("CR_GITLAB_TOKEN", raising=False)
    repo = ConfigRepository(engine, encryption_key=key)
    res = await repo.resolve_forge("gitlab")
    assert res is not None and res.source == "db"
    assert res.url == "https://gl.selfhost.example" and res.token == "k-db"


async def test_resolve_forge_db_empty_url_falls_back_default(engine, monkeypatch):
    key = _fernet_key()
    await _seed_forge(engine, provider="github", url="", token_encrypted=encrypt("t", key))
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    repo = ConfigRepository(engine, encryption_key=key)
    res = await repo.resolve_forge("github")
    assert res is not None and res.url == DEFAULT_FORGE_URLS["github"]


async def test_resolve_forge_none_when_unconfigured(engine, monkeypatch):
    monkeypatch.delenv("CR_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CR_GITHUB_URL", raising=False)
    repo = ConfigRepository(engine, encryption_key=_fernet_key())
    assert await repo.resolve_forge("github") is None



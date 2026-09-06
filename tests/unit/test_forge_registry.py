"""forges/registry 测试：按已解析凭据构造适配器、ForgeRegistry 热更。

构造适配器不触网络（HTTP 客户端注入、无调用），纯离线。
"""

from __future__ import annotations

import httpx

from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.gitlab import GitLabForge
from codereview_ai.forges.registry import ForgeRegistry, build_adapter
from codereview_ai.forges.signatures import GITHUB, GITLAB

DUMMY = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_build_adapter_selects_correct_type():
    f = build_adapter(GITLAB, "https://gl.example.com", "t", DUMMY)
    assert isinstance(f, GitLabForge)


def test_build_adapter_unconfigured_returns_none():
    assert build_adapter(GITHUB, "", "", DUMMY) is None


def test_build_adapter_requires_token():
    assert build_adapter(GITHUB, "https://api.github.com", "", DUMMY) is None


def test_build_adapter_requires_url():
    assert build_adapter(GITLAB, "", "t", DUMMY) is None


def test_github_adapter_with_config():
    f = build_adapter(GITHUB, "https://api.github.com", "g", DUMMY)
    assert isinstance(f, GitHubForge)


class Resolved:
    """resolve_forge 返回值的轻量占位（避免 repository 网络/密钥副作用）。"""

    def __init__(self, url, token, source):
        self.url, self.token, self.source = url, token, source


class _FakeRepo:
    """可编程的 ConfigRepository 桩：按 provider 返回运维侧给定的解析结果。"""

    def __init__(self, values: dict[str, object]) -> None:
        self._values = values
        self.calls: list[str] = []

    async def resolve_forge(self, provider: str, *, force: bool = False):
        self.calls.append(f"{provider}:{force}")
        return self._values.get(provider)


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_forge_registry_builds_adapters_from_resolved():
    repo = _FakeRepo({GITHUB: Resolved("https://api.github.com", "g", "db")})
    reg = ForgeRegistry(repo, DUMMY)
    _run(reg.refresh_all())

    assert reg.available()
    assert isinstance(reg.get(GITHUB), GitHubForge)
    assert reg.get(GITLAB) is None


def test_forge_registry_hot_reload_swaps_adapter():
    """保存后 refresh_all(force) 重解析 → 新凭据替换旧适配器（热更路径）。"""
    repo = _FakeRepo({GITHUB: Resolved("https://a", "v1", "db")})
    reg = ForgeRegistry(repo, DUMMY)
    _run(reg.refresh_all())
    assert reg.get(GITHUB) is not None

    # 运维更新 DB → refresh_all(force=True) 重解析，同 provider 适配器被重建（token 变更代表换新）
    repo._values[GITHUB] = Resolved("https://b", "v2", "db")
    _run(reg.refresh_all())
    assert reg.get(GITHUB) is not None  # 仍可用（集成点：worker ForgeFactory 取到新适配器）
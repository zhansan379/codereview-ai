"""平台适配器注册表：按 provider 构造适配器，支撑双平台 inline 评论（DESIGN §9）。

- `registered_providers(settings)`：按 env 配置（settings 里的 token/url）判断某 provider
  是否可用（旧路径，供 test/env 门控参考）。
- `build_adapter(provider, url, token, http)`：按**已解析凭据**构造适配器；URL/token 缺一
  即返回 None（该平台不注册，worker 对之跳过而非报错）。
- `ForgeRegistry`：持有 from DB/env 解析出的适配器，`refresh_all()` **热更**（保存后无需
  重启，见 main.py）。
"""

from __future__ import annotations

import logging

import httpx

from codereview_ai.config import Settings
from codereview_ai.config.repository import ConfigRepository
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.gitlab import GitLabForge
from codereview_ai.forges.signatures import GITHUB, GITLAB

logger = logging.getLogger("codereview_ai.forge_registry")

#: 有适配器的平台（配置页也以此为白名单）
SUPPORTED_PROVIDERS = (GITLAB, GITHUB)


def registered_providers(settings: Settings) -> list[str]:
    """按 env 配置返回可用的平台列表（有 token 才算注册）。"""
    out: list[str] = []
    if settings.gitlab_token and settings.gitlab_url:
        out.append(GITLAB)
    if settings.github_token and settings.github_url:
        out.append(GITHUB)
    return out


def build_adapter(
    provider: str, url: str, token: str, http: httpx.AsyncClient
) -> ForgeAdapter | None:
    """按已解析凭据构造对应适配器；URL/token 任一缺失返回 None（不注册）。"""
    if provider == GITLAB and url and token:
        return GitLabForge(url, token, http)
    if provider == GITHUB and url and token:
        return GitHubForge(url, token, http)
    return None


class ForgeRegistry:
    """平台适配器热更注册表。

    worker 的 `ForgeFactory` 是同步 `Callable[[str], ForgeAdapter | None]`，不能现场 await；
    这里由 `refresh_all()`（异步）重解析 DB/env 并就地重建 `_adapters`，`get(provider)` 则同步
    读当前版本，**保存配置后热更、无需重启**。构建适配器仅持有 http 客户端（无网络）故廉价；
    单 asyncio 事件循环 + GIL 下，整表替换后读无害。
    """

    def __init__(self, repo: ConfigRepository, http: httpx.AsyncClient) -> None:
        self._repo = repo
        self._http = http
        self._adapters: dict[str, ForgeAdapter] = {}

    async def refresh_all(self) -> None:
        """重解析全部受支持平台，重建适配器表（从 DB / env）。"""
        built: dict[str, ForgeAdapter] = {}
        for provider in SUPPORTED_PROVIDERS:
            resolved = await self._repo.resolve_forge(provider)
            if resolved is None:
                continue
            adapter = build_adapter(provider, resolved.url, resolved.token, self._http)
            if adapter is not None:
                built[provider] = adapter
                logger.info("平台适配器已就绪：%s（source=%s）", provider, resolved.source)
        self._adapters = built

    def get(self, provider: str) -> ForgeAdapter | None:
        """同步读当前已构建的适配器（供 worker 的 ForgeFactory）。"""
        return self._adapters.get(provider)

    def available(self) -> bool:
        """是否有至少一个可用适配器（决定是否启动内置 worker）。"""
        return bool(self._adapters)

    def providers(self) -> list[str]:
        """当前已注册的平台列表（日志/门控用）。"""
        return sorted(self._adapters)
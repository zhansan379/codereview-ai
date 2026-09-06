"""平台适配器注册表：按 provider 构造适配器，支撑双平台 inline 评论（DESIGN §9）。

按配置（settings 里的 token/url）决定某 provider 是否可用：未配置 token 的
平台不注册，worker 对之跳过而非报错。
"""

from __future__ import annotations

import httpx

from codereview_ai.config import Settings
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.forges.github import GitHubForge
from codereview_ai.forges.gitlab import GitLabForge
from codereview_ai.forges.signatures import GITHUB, GITLAB


def registered_providers(settings: Settings) -> list[str]:
    """按配置返回可用的平台列表（有 token 才算注册）。"""
    out: list[str] = []
    if settings.gitlab_token and settings.gitlab_url:
        out.append(GITLAB)
    if settings.github_token and settings.github_url:
        out.append(GITHUB)
    return out


def build_adapter(
    provider: str, settings: Settings, http: httpx.AsyncClient
) -> ForgeAdapter | None:
    """按 provider 构造对应适配器；未配置的 provider 返回 None。"""
    if provider == GITLAB and settings.gitlab_token and settings.gitlab_url:
        return GitLabForge(settings.gitlab_url, settings.gitlab_token, http)
    if provider == GITHUB and settings.github_token and settings.github_url:
        return GitHubForge(settings.github_url, settings.github_token, http)
    return None

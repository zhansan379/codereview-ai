"""平台接入 REST（DESIGN §14.2）：在后台配置 GitHub/GitLab 的 url + token。

`token` 以 Fernet 密文落库（同 model_config），读路径统一回显 `******`；运行时
`ForgeRegistry` 用 `ConfigRepository.resolve_forge`（env 优先、DB 兜底）解析，保存成功后
热更运行中的 worker（无需重启）。

`POST /forges/{provider}/test` 用给定（或当前有效）凭据向平台 API 发一次连接探测，并返回
**能力矩阵**（`probe_capabilities`，见 forges/scopes.py）——按系统所需能力逐项判定
「可用 / 缺权限 / 未知」，让用户看清 token 到底能补拉/拉取、评论、写状态里哪些可用。
探测函数独立可注入，便于离线测试。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

import httpx

from codereview_ai.api.deps import get_current_user, get_db, require_permission
from codereview_ai.config.repository import DEFAULT_FORGE_URLS
from codereview_ai.crypto import MASK, encrypt, is_masked
from codereview_ai.forges.base import repo_path_from_url
from codereview_ai.forges.registry import SUPPORTED_PROVIDERS
from codereview_ai.forges.scopes import Capability, probe_capabilities
from codereview_ai.forges.signatures import GITHUB, GITLAB
from codereview_ai.storage.models import ForgeConfig, _utcnow


def _env_active(provider: str) -> bool:
    """该平台是否有 host env token（有则运行时以 env 为准，压过 DB）。"""
    return bool((os.environ.get(f"CR_{provider.upper()}_TOKEN") or "").strip())


class ForgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    url: str
    token: str  # 读路径回显 ******
    env_active: bool
    enabled: bool = True


class ForgeWrite(BaseModel):
    url: str = ""
    token: str = ""
    enabled: bool = True


class ForgeProbeBody(BaseModel):
    url: str | None = None
    token: str | None = None


def _provider_or_404(provider: str) -> str:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"不支持的平台：{provider}")
    return provider


async def _row_by_provider(session: AsyncSession, provider: str) -> ForgeConfig | None:
    return (await session.execute(
        select(ForgeConfig).where(ForgeConfig.provider == provider)
    )).scalar_one_or_none()


class ResolveRepoBody(BaseModel):
    provider: str = ""
    url: str = ""


class ResolveRepoOut(BaseModel):
    repo_id: str = ""
    repo_full_name: str = ""
    web_url: str = ""


router = APIRouter(
    prefix="/forges",
    dependencies=[Depends(get_current_user), Depends(require_permission("forges:manage"))],
)


@router.get("", response_model=list[ForgeOut])
async def list_forges(session: AsyncSession = Depends(get_db)) -> list[ForgeOut]:
    rows = (await session.execute(
        select(ForgeConfig).order_by(ForgeConfig.provider)
    )).scalars().all()
    by_provider = {r.provider: r for r in rows}
    out: list[ForgeOut] = []
    for p in SUPPORTED_PROVIDERS:
        r = by_provider.get(p)
        env_active = _env_active(p)
        token = MASK if (env_active or (r and r.token_encrypted)) else ""
        out.append(ForgeOut(
            provider=p,
            url=(r.url if r and r.url else "") or DEFAULT_FORGE_URLS.get(p, ""),
            token=token,
            env_active=env_active,
            enabled=(r.enabled if r else True),
        ))
    return out


@router.put("/{provider}", response_model=ForgeOut)
async def update_forge(
    provider: str,
    body: ForgeWrite,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ForgeOut:
    provider = _provider_or_404(provider)
    row = await _row_by_provider(session, provider)
    url = body.url.strip() if body.url else ""
    if not url:
        url = DEFAULT_FORGE_URLS.get(provider, "")
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    if row is None:
        row = ForgeConfig(
            provider=provider,
            url=url,
            token_encrypted=encrypt("" if is_masked(body.token) else body.token, enc_key),
            enabled=body.enabled,
            created_at=_utcnow(),
        )
        session.add(row)
    else:
        row.url = url
        row.enabled = body.enabled
        if not is_masked(body.token):
            row.token_encrypted = encrypt(body.token, enc_key)
    await session.commit()
    await session.refresh(row)
    # 保存即热更运行中的 worker（无需重启）
    reg = getattr(request.app.state, "forge_registry", None)
    if reg is not None:
        await reg.refresh_all()
    return ForgeOut(
        provider=row.provider,
        url=row.url or DEFAULT_FORGE_URLS.get(row.provider, ""),
        token=MASK if row.token_encrypted else "",
        env_active=_env_active(row.provider),
        enabled=row.enabled,
    )


async def probe_forge(provider: str, url: str, token: str) -> list[Capability]:
    """探测平台连通 + 能力矩阵（独立可注入，便于离线测试 monkeypatch）。

    内部委托 `forges.scopes.probe_capabilities`；网络/解析异常向上抛，由端点转 502。
    """
    return await probe_capabilities(provider, url, token)


@router.post("/{provider}/test", response_model=dict)
async def test_forge(
    provider: str,
    body: ForgeProbeBody,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict:
    provider = _provider_or_404(provider)
    url = (body.url or "").strip() or None
    token = (body.token or "").strip() or None
    token_source = "manual" if token else ""
    if not url or not token:
        # 未传凭据 → 用当前有效配置（env 优先、DB 兜底），并记录来源供前端提示
        repo = getattr(request.app.state, "config_repository", None)
        if repo is not None:
            resolved = await repo.resolve_forge(provider)
            if resolved:
                url = url or resolved.url or None
                if not token:
                    token = resolved.token or None
                    token_source = resolved.source
    if not url or not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未配置该平台凭据，无法测试")
    try:
        caps = await probe_forge(provider, url, token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"测试失败: {exc}") from exc
    # 兼容旧前端：`ok` 语义 = 平台连通通过（不必全能力 ok，读/写缺权也先告诉「连上了」）。
    ok = any(c.name == "connect" and c.status == "ok" for c in caps)
    return {"ok": ok, "capabilities": [c.__dict__ for c in caps], "token_source": token_source}


@router.post("/resolve-repo", response_model=ResolveRepoOut)
async def resolve_repo(
    body: ResolveRepoBody,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ResolveRepoOut:
    """从仓库链接解析 {repo_id, repo_full_name, web_url}，供「新增项目」自动回填。

    - GitHub：repo_id = repo_full_name = "owner/name"，纯解析 URL，无需平台凭据；
    - GitLab：repo_id 是数字项目 ID，必须在线调 ``/projects/{path}`` 换回，故用当前已配置的适配器。
    Gitea/Gitee 不在 SUPPORTED_PROVIDERS，前端走本地解析，不在此处理。
    """
    provider = _provider_or_404(body.provider)

    if provider == GITHUB:
        path = repo_path_from_url(body.url, GITHUB)
        if "/" not in path:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该项目 URL")
        return ResolveRepoOut(
            repo_id=path, repo_full_name=path, web_url=body.url.strip().rstrip("/")
        )

    # GITLAB：复用运行中适配器（携带已配置 url+token+http），避免在 api 层新建 client
    registry = getattr(request.app.state, "forge_registry", None)
    adapter = registry.get(GITLAB) if registry else None
    if adapter is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitLab 未接入：请在平台配置填写 GitLab 仓库地址与 Token")
    try:
        meta = await adapter.resolve_repo_meta(body.url) or {}
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"解析失败：{exc}") from exc
    if not meta.get("repo_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该 URL 对应的项目")
    return ResolveRepoOut(**meta)
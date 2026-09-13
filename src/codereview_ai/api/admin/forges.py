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

import asyncio
import os
import urllib.parse
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db, require_permission
from codereview_ai.config.repository import DEFAULT_FORGE_URLS
from codereview_ai.crypto import MASK, encrypt, is_masked
from codereview_ai.forges.base import provider_from_url_host, repo_path_from_url
from codereview_ai.forges.registry import SUPPORTED_PROVIDERS
from codereview_ai.forges.scopes import Capability, probe_capabilities
from codereview_ai.forges.signatures import GITEA, GITEE, GITHUB, GITLAB
from codereview_ai.ops.bootstrap import ensure_worker_started
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
    provider: str = ""  # 留空 → 从 URL 自动识别平台
    url: str = ""


class ResolveRepoOut(BaseModel):
    provider: str = ""
    repo_id: str = ""
    repo_full_name: str = ""
    web_url: str = ""


#: resolve-repo 可解析的平台：Gitea 虽无后端适配器，但仓库 ID 是纯 URL 解析（owner/repo），
#: 与 GitHub/Gitee 同路，不依赖凭据。
_RESOLVABLE_PROVIDERS = (GITHUB, GITLAB, GITEE, GITEA)


async def detect_provider_by_probe(
    url: str, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """对 host 命名看不出平台的站点，探测特征 API 路径识别平台（自建站兜底）。

    三家特征路径互不重叠：Gitea/Gogs ``/api/v1/version``、GitLab ``/api/v4/version``、
    GitHub Enterprise ``/api/v3``。非 404 且非 HTML 页即认为该路径存在——私有站对特征
    路径回 401/403，同样是「平台在这」的证据。并发探测 + 短超时（死站最多等 3s）；
    全不命中返回 ``""``。`transport` 供测试注入 MockTransport；函数整体亦可 monkeypatch。
    """
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates: tuple[tuple[str, str], ...] = (
        (GITEA, "/api/v1/version"),
        (GITLAB, "/api/v4/version"),
        (GITHUB, "/api/v3"),
    )

    async def path_exists(client: httpx.AsyncClient, path: str) -> bool:
        try:
            resp = await client.get(origin + path)
        except httpx.HTTPError:
            return False
        if resp.status_code == 404:
            return False
        # SPA/网关常见「任意路径都 200 回 HTML 首页」——HTML 一律不算特征命中
        return "html" not in resp.headers.get("content-type", "").lower()

    async with httpx.AsyncClient(timeout=3.0, follow_redirects=True, transport=transport) as client:
        hits = await asyncio.gather(*(path_exists(client, path) for _, path in candidates))
    for (provider, _), ok in zip(candidates, hits, strict=True):
        if ok:
            return provider
    return ""


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
    # 配好即生效：模型+平台齐备则现场拉起审查 worker（幂等），无需重启
    await ensure_worker_started(request.app)
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


@router.post("/{provider}/test", response_model=dict[str, Any])
async def test_forge(
    provider: str,
    body: ForgeProbeBody,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
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
    """从仓库链接解析 {provider, repo_id, repo_full_name, web_url}，供「新增项目」免选平台自动回填。

    - body.provider 留空 → 自动识别平台：先按 URL host 映射（公开托管站 + 自建常见命名，
      `provider_from_url_host`），识别不了再探测站点特征 API 路径（`detect_provider_by_probe`）；
      仍失败则 400 请用户手动选平台。显式传入 provider 时以传入值为准（手动兜底路径）。
    - GitHub/Gitee/Gitea：repo_id = repo_full_name = "owner/name"，纯解析 URL，无需平台凭据；
    - GitLab：repo_id 是数字项目 ID，必须在线调 ``/projects/{path}`` 换回，故用当前已配置的适配器。
    """
    given = (body.provider or "").strip().lower()
    if given and given not in _RESOLVABLE_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的平台：{given}")
    provider = given or provider_from_url_host(body.url)
    if not provider and body.url.strip():
        provider = await detect_provider_by_probe(body.url)
    if not provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法从 URL 识别平台，请手动选择")

    if provider in (GITHUB, GITEE, GITEA):
        path = repo_path_from_url(body.url, provider)
        if "/" not in path:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该项目 URL")
        web_url = body.url.strip().rstrip("/")
        return ResolveRepoOut(provider=provider, repo_id=path, repo_full_name=path, web_url=web_url)

    # GITLAB：复用运行中适配器（携带已配置 url+token+http），避免在 api 层新建 client
    registry = getattr(request.app.state, "forge_registry", None)
    adapter = registry.get(GITLAB) if registry else None
    if adapter is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "GitLab 未接入：请在平台配置填写 GitLab 仓库地址与 Token"
        )
    try:
        meta = await adapter.resolve_repo_meta(body.url) or {}
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"解析失败：{exc}") from exc
    if not meta.get("repo_id"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该 URL 对应的项目")
    return ResolveRepoOut(provider=GITLAB, **meta)

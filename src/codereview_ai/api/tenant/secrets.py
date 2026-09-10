"""租户（workspace）秘密管理 REST（BYOK）：租户在自有 workspace 内自配 LLM key + forge PAT。

- `/workspaces/{wid}/models`：空间级模型配置（api_key Fernet 落库、读路径回显 `******`）。
  复用 `api/admin/models.py` 的 DTO（ModelWrite/ModelOut）与 mask 语义。
- `/workspaces/{wid}/forges/{provider}`：空间级 forge 凭据（url + token，upsert）。
- `/workspaces/{wid}/platform-fallback`：超管专属豁免开关——置 True → 该空间无自带凭据时
  回落平台全局凭据（否则「必须自带 key」，缺失直接降级）。

所有端点经 `require_workspace_owner`：超管或该 workspace 的 owner 才能操作。多租户当前无
`workspace_member` 中间表，租户身份即 `Workspace.owner_id`（单用户私有空间）。写路径保存后
`forge_registry.invalidate()` 清按仓库的适配器缓存，worker 下次解析即用新凭据。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.admin.models import ModelOut, ModelWrite
from codereview_ai.api.deps import (
    CurrentUser,
    get_current_user,
    get_db,
    require_workspace_owner,
)
from codereview_ai.config.repository import DEFAULT_FORGE_URLS
from codereview_ai.crypto import MASK, encrypt, is_masked
from codereview_ai.forges.registry import SUPPORTED_PROVIDERS
from codereview_ai.storage.models import WorkspaceForgeConfig, WorkspaceModelConfig, _utcnow

router = APIRouter(
    prefix="/workspaces",
    dependencies=[Depends(get_current_user)],
)


def _enc_key(request: Request) -> str:
    return getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""


def _invalidate_forge_cache(request: Request) -> None:
    reg = getattr(request.app.state, "forge_registry", None)
    if reg is not None:
        reg.invalidate()


# ---------------- workspace 模型配置 ----------------

async def _ws_model_or_404(
    session: AsyncSession, workspace_id: int, model_id: int
) -> WorkspaceModelConfig:
    row = (await session.execute(
        select(WorkspaceModelConfig).where(
            WorkspaceModelConfig.id == model_id,
            WorkspaceModelConfig.workspace_id == workspace_id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型配置不存在")
    return row


def _to_model_out(row: WorkspaceModelConfig) -> ModelOut:
    return ModelOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        api_key=MASK if row.api_key_encrypted else "",
        base_url=row.base_url,
        temperature=row.temperature,
        max_tokens=row.max_tokens,
        capabilities=list(row.capabilities) if isinstance(row.capabilities, (list, dict)) else [],
        priority=row.priority,
        enabled=row.enabled,
    )


@router.get("/{workspace_id}/models", response_model=list[ModelOut])
async def list_workspace_models(
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> list[ModelOut]:
    rows = (await session.execute(
        select(WorkspaceModelConfig)
        .where(WorkspaceModelConfig.workspace_id == workspace_id)
        .order_by(WorkspaceModelConfig.priority, WorkspaceModelConfig.id)
    )).scalars().all()
    return [_to_model_out(r) for r in rows]


@router.post("/{workspace_id}/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def create_workspace_model(
    body: ModelWrite,
    request: Request,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> ModelOut:
    if is_masked(body.api_key):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新建时 api_key 不能为 ******")
    row = WorkspaceModelConfig(
        workspace_id=workspace_id,
        **body.model_dump(exclude={"api_key"}),
        api_key_encrypted=encrypt(body.api_key, _enc_key(request)),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_model_out(row)


@router.put("/{workspace_id}/models/{model_id}", response_model=ModelOut)
async def update_workspace_model(
    model_id: int,
    body: ModelWrite,
    request: Request,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> ModelOut:
    row = await _ws_model_or_404(session, workspace_id, model_id)
    data = body.model_dump(exclude={"api_key"})
    for field, value in data.items():
        if value is None:
            continue
        setattr(row, field, value)
    if not is_masked(body.api_key):
        row.api_key_encrypted = encrypt(body.api_key, _enc_key(request))
    await session.commit()
    await session.refresh(row)
    return _to_model_out(row)


@router.delete("/{workspace_id}/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_model(
    model_id: int,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> None:
    row = await _ws_model_or_404(session, workspace_id, model_id)
    await session.delete(row)
    await session.commit()


# ---------------- workspace forge 凭据 ----------------

class WorkspaceForgeOut(BaseModel):
    provider: str
    url: str
    token: str  # 读路径回显 ******
    enabled: bool = True


class WorkspaceForgeWrite(BaseModel):
    url: str = ""
    token: str = ""
    enabled: bool = True


def _provider_or_404(provider: str) -> str:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"不支持的平台：{provider}")
    return provider


async def _ws_forge_or_none(
    session: AsyncSession, workspace_id: int, provider: str
) -> WorkspaceForgeConfig | None:
    return (await session.execute(
        select(WorkspaceForgeConfig).where(
            WorkspaceForgeConfig.workspace_id == workspace_id,
            WorkspaceForgeConfig.provider == provider,
        )
    )).scalar_one_or_none()


@router.get("/{workspace_id}/forges/{provider}", response_model=WorkspaceForgeOut)
async def get_workspace_forge(
    provider: str,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceForgeOut:
    provider = _provider_or_404(provider)
    row = await _ws_forge_or_none(session, workspace_id, provider)
    if row is None:
        return WorkspaceForgeOut(provider=provider, url="", token="", enabled=True)
    return WorkspaceForgeOut(
        provider=row.provider,
        url=row.url or DEFAULT_FORGE_URLS.get(provider, ""),
        token=MASK if row.token_encrypted else "",
        enabled=row.enabled,
    )


@router.put("/{workspace_id}/forges/{provider}", response_model=WorkspaceForgeOut)
async def put_workspace_forge(
    provider: str,
    body: WorkspaceForgeWrite,
    request: Request,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> WorkspaceForgeOut:
    provider = _provider_or_404(provider)
    url = body.url.strip() if body.url else DEFAULT_FORGE_URLS.get(provider, "")
    row = await _ws_forge_or_none(session, workspace_id, provider)
    if row is None:
        row = WorkspaceForgeConfig(
            workspace_id=workspace_id,
            provider=provider,
            url=url,
            token_encrypted=encrypt("" if is_masked(body.token) else body.token, _enc_key(request)),
            enabled=body.enabled,
            created_at=_utcnow(),
        )
        session.add(row)
    else:
        row.url = url
        row.enabled = body.enabled
        if not is_masked(body.token):
            row.token_encrypted = encrypt(body.token, _enc_key(request))
    await session.commit()
    await session.refresh(row)
    _invalidate_forge_cache(request)
    return WorkspaceForgeOut(
        provider=row.provider,
        url=row.url or DEFAULT_FORGE_URLS.get(provider, ""),
        token=MASK if row.token_encrypted else "",
        enabled=row.enabled,
    )


@router.delete("/{workspace_id}/forges/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_forge(
    provider: str,
    request: Request,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> None:
    provider = _provider_or_404(provider)
    row = await _ws_forge_or_none(session, workspace_id, provider)
    if row is not None:
        await session.delete(row)
        await session.commit()
        _invalidate_forge_cache(request)


# ---------------- 平台豁免开关（超管专属） ----------------

class PlatformFallbackWrite(BaseModel):
    enabled: bool = False


@router.get("/{workspace_id}/platform-fallback", response_model=dict[str, Any])
async def get_platform_fallback(
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """读豁免开关（超管或 owner 可读，供设置页回显）。"""
    from codereview_ai.storage.models import Workspace

    ws = (await session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()  # noqa: E501
    return {"workspace_id": workspace_id, "platform_fallback": ws.platform_fallback}


@router.put("/{workspace_id}/platform-fallback", response_model=dict[str, Any])
async def set_platform_fallback(
    body: PlatformFallbackWrite,
    request: Request,
    user: CurrentUser,
    workspace_id: int = Depends(require_workspace_owner),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not user.role.is_super:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有平台超管可以设置豁免")
    from codereview_ai.storage.models import Workspace

    ws = (await session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()  # noqa: E501
    ws.platform_fallback = body.enabled
    await session.commit()
    _invalidate_forge_cache(request)
    return {"workspace_id": workspace_id, "platform_fallback": ws.platform_fallback}

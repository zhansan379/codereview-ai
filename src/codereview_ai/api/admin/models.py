"""模型配置 REST（DESIGN §14.2，F5.3）：注册模型 + api_key Fernet 加密 + 连通测试。

api_key 写路径加密落库（`api_key_encrypted`），读路径统一回显 `******`（DESIGN §16）。
`POST /models/{id}/test` 用该模型发一条最少请求探测连通性；探测函数可注入便于离线测试。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.crypto import MASK, decrypt, encrypt, is_masked
from codereview_ai.review.llm_gateway import LLMGateway
from codereview_ai.storage.models import ModelConfig

router = APIRouter(prefix="/models", dependencies=[Depends(get_current_user)])


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    model: str
    api_key: str
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    capabilities: list[str] = []
    priority: int = 0
    enabled: bool = True


class ModelWrite(BaseModel):
    name: str = ""
    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    capabilities: list[str] = []
    priority: int = 0
    enabled: bool = True


#: 最小的连通性探测请求体（带到后台由服务端自行组装真实请求）
class ProbeRequest(BaseModel):
    prompt: str = "ping"


async def _to_out(row: ModelConfig) -> ModelOut:
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


async def _get_or_404(session: AsyncSession, model_id: int) -> ModelConfig:
    row = (await session.execute(select(ModelConfig).where(ModelConfig.id == model_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型配置不存在")
    return row


@router.get("", response_model=list[ModelOut])
async def list_models(session: AsyncSession = Depends(get_db)) -> list[ModelOut]:
    rows = (await session.execute(select(ModelConfig).order_by(ModelConfig.priority, ModelConfig.id))).scalars().all()  # noqa: E501
    return [await _to_out(r) for r in rows]


@router.post("", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelWrite, request: Request, session: AsyncSession = Depends(get_db)
) -> ModelOut:
    api_key = body.api_key
    if api_key == MASK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新建时 api_key 不能为 ******")
    key = getattr(request.app.state, "settings", None)
    enc_key = getattr(key, "encryption_key", "") or ""
    row = ModelConfig(
        **body.model_dump(exclude={"api_key"}),
        api_key_encrypted=encrypt(body.api_key, enc_key),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _to_out(row)


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(model_id: int, session: AsyncSession = Depends(get_db)) -> ModelOut:
    row = await _get_or_404(session, model_id)
    return await _to_out(row)


@router.put("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int, body: ModelWrite, request: Request, session: AsyncSession = Depends(get_db)
) -> ModelOut:
    row = await _get_or_404(session, model_id)
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    data = body.model_dump(exclude={"api_key"})
    for field, value in data.items():
        if value is None:  # 可选字段不填视为「保留原值」；避免 None 覆盖非空列
            continue
        setattr(row, field, value)
    if is_masked(body.api_key):
        pass  # ****** 表示保留原文不动
    else:
        row.api_key_encrypted = encrypt(body.api_key, enc_key)
    await session.commit()
    await session.refresh(row)
    return await _to_out(row)


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_or_404(session, model_id)
    await session.delete(row)
    await session.commit()


async def probe_model(cfg: ModelConfig, prompt: str, *, encryption_key: str) -> bool:
    """发一条最少 LLM 请求探测连通性。

    通过 `LLMGateway` 带着解密后的 api_key 发一条极简请求，能拿到非空文本即连通。
    环境变量（`CR_*`）作为 LLM 客户端配置来源，探测失败统一抛错由上层转 502。
    测试可 monkeypatch 本函数离线断言，无需真实网络。
    """
    if not cfg.api_key_encrypted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该模型未配置 api_key，无法探测")
    api_key = decrypt(cfg.api_key_encrypted, encryption_key)
    kv: dict[str, str] = {}
    if cfg.provider:
        kv[f"{cfg.provider}_api_key"] = api_key
    if cfg.base_url:
        kv["api_base"] = cfg.base_url
    with patch_env(kv):
        gateway = LLMGateway(model=cfg.model or cfg.name)
        text = await gateway.complete([{"role": "user", "content": prompt}])
    return bool(text.strip())


@contextmanager
def patch_env(kv: dict[str, str], env: dict[str, str] | None = None) -> Any:
    """临时写入 LLM 客户端依赖的 env 配置，块结束还原。env 缺失时透传现环境。"""
    import os

    saved: dict[str, str | None] = {}
    for key, value in kv.items():
        saved[key] = env.get(key) if env is not None else os.environ.get(key)
        if env is not None:
            env[key] = value
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in saved.items():
            if env is not None:
                if old is None:
                    env.pop(key, None)
                else:
                    env[key] = old
            else:
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old


@router.post("/{model_id}/test", response_model=dict[str, Any])
async def test_model(
    model_id: int,
    body: ProbeRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await _get_or_404(session, model_id)
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    if not enc_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务端加密密钥未配置")
    try:
        await probe_model(row, body.prompt, encryption_key=enc_key)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"探测失败: {exc}") from exc
    return {"ok": True, "model_id": row.id, "prompt": body.prompt}

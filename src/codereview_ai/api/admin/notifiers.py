"""IM 通知渠道 REST（DESIGN §14.2，F4）：配置钉钉/飞书/企微 + 项目路由。

webhook 与签名 secret 均以 Fernet 密文落库（DESIGN §16），读路径统一回显 `******`；
`project_id` 为 NULL 表示全局默认，非空为项目级覆盖。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import (
    CurrentUser,
    allowed_project_ids,
    get_current_user,
    get_db,
    require_permission,
)
from codereview_ai.crypto import MASK, encrypt, is_masked
from codereview_ai.storage.models import NotifierConfig

router = APIRouter(prefix="/notifiers", dependencies=[Depends(get_current_user)])


class NotifierOut(BaseModel):
    id: int
    channel: str
    enabled: bool
    webhook: str
    secret: str
    project_id: int | None
    at_threshold: int


class NotifierWrite(BaseModel):
    channel: str = ""
    enabled: bool = True
    webhook: str = ""
    secret: str = ""
    project_id: int | None = None
    at_threshold: int = 60


def _to_out(row: NotifierConfig, enc_key: str) -> NotifierOut:
    return NotifierOut(
        id=row.id,
        channel=row.channel,
        enabled=row.enabled,
        webhook=MASK if row.webhook_encrypted else "",
        secret=MASK if row.secret_encrypted else "",
        project_id=row.project_id,
        at_threshold=row.at_threshold,
    )


async def _get_or_404(session: AsyncSession, notifier_id: int) -> NotifierConfig:
    row = (await session.execute(select(NotifierConfig).where(NotifierConfig.id == notifier_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "通知渠道不存在")
    return row


@router.get("", response_model=list[NotifierOut])
async def list_notifiers(
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> list[NotifierOut]:
    stmt = select(NotifierConfig)
    is_global, ids = await allowed_project_ids(session, user)
    if not is_global:
        # 全局默认渠道全局可见；项目级覆盖仅其成员可见
        stmt = stmt.where(or_(NotifierConfig.project_id.is_(None),
                              NotifierConfig.project_id.in_(list(ids))))
    rows = (await session.execute(stmt.order_by(NotifierConfig.id))).scalars().all()
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    return [_to_out(r, enc_key) for r in rows]


@router.post("", response_model=NotifierOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("notifiers:manage"))])
async def create_notifier(
    body: NotifierWrite, request: Request, session: AsyncSession = Depends(get_db)
) -> NotifierOut:
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    if body.webhook == MASK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新建时 webhook 不能为 ******")
    row = NotifierConfig(
        channel=body.channel,
        enabled=body.enabled,
        webhook_encrypted=encrypt(body.webhook, enc_key),
        secret_encrypted=encrypt(body.secret, enc_key),
        project_id=body.project_id,
        at_threshold=body.at_threshold,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row, enc_key)


@router.get("/{notifier_id}", response_model=NotifierOut)
async def get_notifier(
    notifier_id: int,
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> NotifierOut:
    row = await _get_or_404(session, notifier_id)
    is_global, ids = await allowed_project_ids(session, user)
    if not is_global and row.project_id is not None and row.project_id not in ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "通知渠道不存在")
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    return _to_out(row, enc_key)


@router.put("/{notifier_id}", response_model=NotifierOut,
            dependencies=[Depends(require_permission("notifiers:manage"))])
async def update_notifier(
    notifier_id: int, body: NotifierWrite, request: Request, session: AsyncSession = Depends(get_db)
) -> NotifierOut:
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    row = await _get_or_404(session, notifier_id)
    for field in ("channel", "enabled", "project_id", "at_threshold"):
        setattr(row, field, getattr(body, field))
    if not is_masked(body.webhook):
        row.webhook_encrypted = encrypt(body.webhook, enc_key)
    if not is_masked(body.secret):
        row.secret_encrypted = encrypt(body.secret, enc_key)
    await session.commit()
    await session.refresh(row)
    return _to_out(row, enc_key)


@router.delete("/{notifier_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_permission("notifiers:manage"))])
async def delete_notifier(notifier_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_or_404(session, notifier_id)
    await session.delete(row)
    await session.commit()

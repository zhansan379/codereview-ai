"""IM 通知渠道 REST（DESIGN §14.2，F4）：配置钉钉/飞书/企微 + 项目路由。

webhook 与签名 secret 均以 Fernet 密文落库（DESIGN §16），读路径统一回显 `******`；
`project_id` 为 NULL 表示全局默认，非空为项目级覆盖。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_current_user, get_db
from codereview_ai.crypto import MASK, encrypt, is_masked
from codereview_ai.storage.models import NotifierConfig, NotifierRouteMember

router = APIRouter(prefix="/notifiers", dependencies=[Depends(get_current_user)])


class NotifierOut(BaseModel):
    id: int
    channel: str
    enabled: bool
    webhook: str
    secret: str
    project_id: int | None
    at_threshold: int
    at_member_ids: list[int] = []


class NotifierWrite(BaseModel):
    channel: str = ""
    enabled: bool = True
    webhook: str = ""
    secret: str = ""
    project_id: int | None = None
    at_threshold: int = 60
    at_member_ids: list[int] = []


async def _member_ids(session: AsyncSession, notifier_id: int) -> list[int]:
    rows = (await session.execute(
        select(NotifierRouteMember.member_id)
        .where(NotifierRouteMember.notifier_id == notifier_id)
    )).scalars().all()
    return list(rows)


async def _sync_members(
    session: AsyncSession, notifier_id: int, member_ids: list[int]
) -> None:
    """重写渠道 × 成员 绑定（去重）；删除渠道时显式清空绑定行。"""
    await session.execute(
        delete(NotifierRouteMember).where(NotifierRouteMember.notifier_id == notifier_id)
    )
    for mid in dict.fromkeys(member_ids):
        session.add(NotifierRouteMember(notifier_id=notifier_id, member_id=mid))


def _to_out(row: NotifierConfig, enc_key: str, member_ids: list[int] | None = None) -> NotifierOut:
    return NotifierOut(
        id=row.id,
        channel=row.channel,
        enabled=row.enabled,
        webhook=MASK if row.webhook_encrypted else "",
        secret=MASK if row.secret_encrypted else "",
        project_id=row.project_id,
        at_threshold=row.at_threshold,
        at_member_ids=member_ids or [],
    )


async def _get_or_404(session: AsyncSession, notifier_id: int) -> NotifierConfig:
    row = (await session.execute(select(NotifierConfig).where(NotifierConfig.id == notifier_id))).scalar_one_or_none()  # noqa: E501
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "通知渠道不存在")
    return row


@router.get("", response_model=list[NotifierOut])
async def list_notifiers(
    request: Request, session: AsyncSession = Depends(get_db)
) -> list[NotifierOut]:
    rows = (await session.execute(select(NotifierConfig).order_by(NotifierConfig.id))).scalars().all()  # noqa: E501
    links = (await session.execute(select(NotifierRouteMember))).scalars().all()
    by_notifier: dict[int, list[int]] = {}
    for link in links:
        by_notifier.setdefault(link.notifier_id, []).append(link.member_id)
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    return [_to_out(r, enc_key, by_notifier.get(r.id, [])) for r in rows]


@router.post("", response_model=NotifierOut, status_code=status.HTTP_201_CREATED)
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
    await session.flush()  # 先拿到 notifier.id 供绑定
    await _sync_members(session, row.id, list(body.at_member_ids))
    await session.commit()
    await session.refresh(row)
    return _to_out(row, enc_key, list(body.at_member_ids))


@router.get("/{notifier_id}", response_model=NotifierOut)
async def get_notifier(
    notifier_id: int, request: Request, session: AsyncSession = Depends(get_db)
) -> NotifierOut:
    row = await _get_or_404(session, notifier_id)
    enc_key = getattr(getattr(request.app.state, "settings", None), "encryption_key", "") or ""
    return _to_out(row, enc_key, await _member_ids(session, notifier_id))


@router.put("/{notifier_id}", response_model=NotifierOut)
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
    await _sync_members(session, notifier_id, list(body.at_member_ids))
    await session.commit()
    await session.refresh(row)
    return _to_out(row, enc_key, list(body.at_member_ids))


@router.delete("/{notifier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notifier(notifier_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_or_404(session, notifier_id)
    await _sync_members(session, notifier_id, [])  # 清理绑定行（项目无外键，显式删）
    await session.delete(row)
    await session.commit()

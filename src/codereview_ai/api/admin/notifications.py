"""系统消息提醒 REST（通用）。

- `GET /notifications`：查询未确认的消息列表（前端轮询展示）
- `POST /notifications/{id}/acknowledge`：标记指定消息为已确认
- `POST /notifications/acknowledge-all`：标记所有未确认消息为已确认
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from codereview_ai.api.deps import get_current_user, require_permission
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.system_notification_repo import SystemNotificationRepository

router = APIRouter(
    prefix="/notifications",
    dependencies=[Depends(get_current_user), Depends(require_permission("settings:manage"))],
)


class NotificationOut(BaseModel):
    id: int
    type: str
    level: str
    title: str
    message: str
    extra_data: dict
    acknowledged: bool
    acknowledged_at: datetime | None
    created_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int


class AcknowledgeResult(BaseModel):
    acknowledged: int


@router.get("", response_model=NotificationListOut)
async def list_notifications(request: Request) -> NotificationListOut:
    """查询未确认的系统消息列表。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = SystemNotificationRepository(session)
        items = await repo.list_unacknowledged()
        return NotificationListOut(
            items=[
                NotificationOut(
                    id=item.id,
                    type=item.type,
                    level=item.level,
                    title=item.title,
                    message=item.message,
                    extra_data=item.extra_data,
                    acknowledged=item.acknowledged,
                    acknowledged_at=item.acknowledged_at,
                    created_at=item.created_at,
                )
                for item in items
            ],
            total=len(items),
        )


@router.post("/{notification_id}/acknowledge", response_model=AcknowledgeResult)
async def acknowledge_notification(
    notification_id: int, request: Request
) -> AcknowledgeResult:
    """标记指定系统消息为已确认。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = SystemNotificationRepository(session)
        success = await repo.acknowledge(notification_id)
        await session.commit()
        return AcknowledgeResult(acknowledged=1 if success else 0)


@router.post("/acknowledge-all", response_model=AcknowledgeResult)
async def acknowledge_all_notifications(request: Request) -> AcknowledgeResult:
    """标记所有未确认的系统消息为已确认。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = SystemNotificationRepository(session)
        count = await repo.acknowledge_all()
        await session.commit()
        return AcknowledgeResult(acknowledged=count)

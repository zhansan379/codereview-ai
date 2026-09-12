"""系统消息提醒 REST（通用）。

- `GET /notifications`：查询未确认的消息列表（前端轮询展示）
- `GET /notifications/stream`：SSE 实时推送新消息（单独路由，通过 query 参数认证）
- `POST /notifications/{id}/acknowledge`：标记指定消息为已确认
- `POST /notifications/acknowledge-all`：标记所有未确认消息为已确认
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from codereview_ai.api.deps import get_current_user, require_permission
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.system_notification_repo import SystemNotificationRepository

router = APIRouter(
    prefix="/notifications",
    dependencies=[Depends(get_current_user), Depends(require_permission("settings:manage"))],
)

# SSE 单独路由（不带全局认证，因为 EventSource 不支持自定义 header）
sse_router = APIRouter(prefix="/notifications")


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


# ===== SSE 广播器 =====
class NotificationBroadcaster:
    """管理所有 SSE 连接，新消息创建时广播给所有客户端。"""

    def __init__(self) -> None:
        self._clients: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._clients.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._clients:
            self._clients.remove(queue)

    async def broadcast(self, notification: dict) -> None:
        for queue in self._clients:
            await queue.put(notification)


# 全局广播器实例
broadcaster = NotificationBroadcaster()


@sse_router.get("/stream")
async def stream_notifications(request: Request, token: str = "") -> StreamingResponse:
    """SSE 实时推送新消息。通过 query 参数传递 token（EventSource 不支持自定义 header）。"""
    # 验证 token
    import jwt

    from codereview_ai.config import Settings

    settings = Settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid token")

    if not payload.get("sub"):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid token")

    async def event_generator() -> AsyncGenerator[str, None]:
        queue = broadcaster.subscribe()
        try:
            while True:
                # 检查客户端是否断开连接
                if await request.is_disconnected():
                    break
                try:
                    # 等待新消息，超时 30 秒发送心跳
                    notification = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(notification, default=str)}\n\n"
                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield ": heartbeat\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


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


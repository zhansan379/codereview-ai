"""系统消息提醒 REST（通用，登录即可用）。

- `GET /notifications`：查询未确认的消息列表（前端轮询展示）
- `GET /notifications/stream`：SSE 实时推送新消息（单独路由，通过 query 参数认证）
- `POST /notifications/{id}/acknowledge`：标记指定消息为已确认
- `POST /notifications/acknowledge-all`：标记所有未确认消息为已确认

权限口径：**任何登录用户**可读/确认——这是「后台异步结果 → 用户」的提醒通道（重发成败、
webhook 配置错误等），消息全局广播、不挑收件人，SSE 流本就只验 token；此前挂
`settings:manage` 时，实际触发重发的 tech_lead 反而收不到自己操作的结果。消息内容不含
敏感配置（webhook 地址不带密钥，鉴权靠签名），不构成越权面。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from codereview_ai.api.deps import get_current_user
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.system_notification_repo import SystemNotificationRepository

router = APIRouter(prefix="/notifications", dependencies=[Depends(get_current_user)])

# SSE 单独路由（不带全局认证，因为 EventSource 不支持自定义 header）
sse_router = APIRouter(prefix="/notifications")


class NotificationOut(BaseModel):
    id: int
    type: str
    level: str
    title: str
    message: str
    extra_data: dict[str, Any]
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
        self._clients: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._clients.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._clients:
            self._clients.remove(queue)

    async def broadcast(self, notification: dict[str, Any]) -> None:
        for queue in self._clients:
            await queue.put(notification)


# 全局广播器实例
broadcaster = NotificationBroadcaster()


@sse_router.get("/stream")
async def stream_notifications(request: Request, token: str = "") -> StreamingResponse:
    """SSE 实时推送新消息。通过 query 参数传递 token（EventSource 不支持自定义 header）。

    连接建立时验 token + 按库校验用户可用（禁用即时拒之门外，与 REST 侧同语义；
    长连接期间再被禁用的，断线重连时生效）。
    """
    # 验证 token
    import jwt
    from fastapi import HTTPException
    from sqlalchemy import select as _select

    from codereview_ai.config import Settings
    from codereview_ai.storage.models import User

    settings = getattr(request.app.state, "settings", None) or Settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        try:
            uid = int(sub)
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Invalid token") from None
        async with session_factory(engine)() as session:
            user = (await session.execute(
                _select(User).where(User.id == uid)
            )).scalar_one_or_none()
        if user is None or not user.enabled:
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
                except TimeoutError:
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


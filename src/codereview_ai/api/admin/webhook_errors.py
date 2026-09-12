"""Webhook 配置错误记录 REST（持久化提醒）。

- `GET /webhook-errors`：查询未确认的错误列表（前端轮询展示）
- `POST /webhook-errors/{id}/acknowledge`：标记指定错误为已确认
- `POST /webhook-errors/acknowledge-all`：标记所有未确认错误为已确认
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from codereview_ai.api.deps import get_current_user, require_permission
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.webhook_error_repo import WebhookErrorRepository

router = APIRouter(
    prefix="/webhook-errors",
    dependencies=[Depends(get_current_user), Depends(require_permission("settings:manage"))],
)


class WebhookErrorOut(BaseModel):
    id: int
    provider: str
    wrong_url: str
    correct_url: str
    source_ip: str
    acknowledged: bool
    acknowledged_at: datetime | None
    created_at: datetime


class WebhookErrorListOut(BaseModel):
    items: list[WebhookErrorOut]
    total: int


class AcknowledgeResult(BaseModel):
    acknowledged: int


@router.get("", response_model=WebhookErrorListOut)
async def list_webhook_errors(request: Request) -> WebhookErrorListOut:
    """查询未确认的 webhook 配置错误列表。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = WebhookErrorRepository(session)
        items = await repo.list_unacknowledged()
        return WebhookErrorListOut(
            items=[
                WebhookErrorOut(
                    id=item.id,
                    provider=item.provider,
                    wrong_url=item.wrong_url,
                    correct_url=item.correct_url,
                    source_ip=item.source_ip,
                    acknowledged=item.acknowledged,
                    acknowledged_at=item.acknowledged_at,
                    created_at=item.created_at,
                )
                for item in items
            ],
            total=len(items),
        )


@router.post("/{error_id}/acknowledge", response_model=AcknowledgeResult)
async def acknowledge_webhook_error(error_id: int, request: Request) -> AcknowledgeResult:
    """标记指定 webhook 配置错误为已确认。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = WebhookErrorRepository(session)
        success = await repo.acknowledge(error_id)
        await session.commit()
        return AcknowledgeResult(acknowledged=1 if success else 0)


@router.post("/acknowledge-all", response_model=AcknowledgeResult)
async def acknowledge_all_webhook_errors(request: Request) -> AcknowledgeResult:
    """标记所有未确认的 webhook 配置错误为已确认。"""
    engine = request.app.state.engine
    async with session_factory(engine)() as session:
        repo = WebhookErrorRepository(session)
        count = await repo.acknowledge_all()
        await session.commit()
        return AcknowledgeResult(acknowledged=count)

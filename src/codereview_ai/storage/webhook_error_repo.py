"""Webhook 配置错误记录仓库（持久化提醒）。

职责：
- 记录 webhook 路径配置错误（平台发来的 webhook 请求但路径不对）
- 查询未确认的错误列表（前端轮询展示）
- 标记错误为已确认（用户点击确认后）
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.storage.models import WebhookError


class WebhookErrorRepository:
    """Webhook 配置错误记录的增删查改。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        provider: str,
        wrong_url: str,
        correct_url: str,
        source_ip: str = "",
    ) -> WebhookError:
        """记录一条 webhook 配置错误。相同 wrong_url 和 provider 的错误 1 分钟内不重复插入。"""
        # 检查是否已存在相同的错误记录（1 分钟内）
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import select

        threshold = datetime.now(UTC) - timedelta(minutes=1)
        stmt = (
            select(WebhookError)
            .where(
                WebhookError.provider == provider,
                WebhookError.wrong_url == wrong_url,
                WebhookError.created_at >= threshold,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        row = WebhookError(
            provider=provider,
            wrong_url=wrong_url,
            correct_url=correct_url,
            source_ip=source_ip,
            acknowledged=False,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_unacknowledged(self, limit: int = 50) -> list[WebhookError]:
        """查询未确认的错误列表（按创建时间倒序）。"""
        stmt = (
            select(WebhookError)
            .where(WebhookError.acknowledged == False)  # noqa: E712
            .order_by(WebhookError.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def acknowledge(self, error_id: int) -> bool:
        """标记指定错误为已确认。返回是否成功更新。"""
        stmt = (
            update(WebhookError)
            .where(WebhookError.id == error_id)
            .values(acknowledged=True, acknowledged_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def acknowledge_all(self) -> int:
        """标记所有未确认错误为已确认。返回更新行数。"""
        stmt = (
            update(WebhookError)
            .where(WebhookError.acknowledged == False)  # noqa: E712
            .values(acknowledged=True, acknowledged_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

"""系统消息提醒仓库（通用）。

职责：
- 记录各类系统消息（webhook 配置错误、系统异常等）
- 查询未确认的消息列表（前端轮询展示）
- 标记消息为已确认（用户点击确认后）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.storage.models import SystemNotification


class SystemNotificationRepository:
    """系统消息提醒的增删查改。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        type: str,
        title: str,
        message: str = "",
        level: str = "info",
        extra_data: dict[str, object] | None = None,
    ) -> SystemNotification:
        """记录一条系统消息。相同 type + title 的消息 1 分钟内不重复插入。"""
        # 去重检查：1 分钟内相同 type + title 的消息不重复插入
        threshold = datetime.now(UTC) - timedelta(minutes=1)
        stmt = (
            select(SystemNotification)
            .where(
                SystemNotification.type == type,
                SystemNotification.title == title,
                SystemNotification.created_at >= threshold,
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        row = SystemNotification(
            type=type,
            title=title,
            message=message,
            level=level,
            extra_data=extra_data or {},
            acknowledged=False,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_unacknowledged(self, limit: int = 50) -> list[SystemNotification]:
        """查询未确认的消息列表（按创建时间倒序）。"""
        stmt = (
            select(SystemNotification)
            .where(SystemNotification.acknowledged == False)  # noqa: E712
            .order_by(SystemNotification.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def acknowledge(self, notification_id: int) -> bool:
        """标记指定消息为已确认。返回是否成功更新。"""
        stmt = (
            update(SystemNotification)
            .where(SystemNotification.id == notification_id)
            .values(acknowledged=True, acknowledged_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def acknowledge_all(self) -> int:
        """标记所有未确认消息为已确认。返回更新行数。"""
        stmt = (
            update(SystemNotification)
            .where(SystemNotification.acknowledged == False)  # noqa: E712
            .values(acknowledged=True, acknowledged_at=datetime.now(UTC))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount

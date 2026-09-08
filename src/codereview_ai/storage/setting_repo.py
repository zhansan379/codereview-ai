"""全局运行时设置存取（`app_setting` key-value 表）。

供后台可热更参数落库/读取，如 worker 并发数。每次调用独立短会话（同 ProjectRepository），
无 TTL 缓存——配置落库即时读得，呼应热更理念；重启后按库值生效。
"""

from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import AppSetting

#: push 轨全局默认开关的键（值 "1"/"0"）。worker 按 push 事件热读，缺行则回落到 env
#: `CR_PUSH_REVIEW_ENABLED`；项目级 `push_enabled` 仍可覆盖（follow/enforce 双态）。
PUSH_REVIEW_DEFAULT_KEY = "push_review_default"

#: MR 轨全局默认开关的键（值 "1"/"0"）。worker 按 MR 事件热读，缺行则回落到 env
#: `CR_MR_REVIEW_ENABLED`；项目级 `mr_enabled` 仍可覆盖（与 push 轨道同法，DESIGN 双轨对称）。
MR_REVIEW_DEFAULT_KEY = "mr_review_default"


class SettingRepository:
    """`app_setting` 表读取/写入；值一律存字符串，调用方按需转换。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, key: str) -> str | None:
        """读指定键；不存在返回 None。"""
        from sqlalchemy import select

        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(AppSetting).where(AppSetting.key == key)
            )).scalar_one_or_none()
        return row.value if row is not None else None

    async def set(self, key: str, value: str) -> None:
        """写指定键（存在则覆盖）。事务内 upsert，幂等。"""
        stmt = sqlite_insert(AppSetting).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
        session = session_factory(self._engine)
        async with session() as s:
            await s.execute(stmt)
            await s.commit()

    async def get_int(self, key: str, default: int) -> int:
        """读整数键；缺省/非法 → `default`。"""
        raw = await self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    async def get_bool_optional(self, key: str) -> bool | None:
        """读布尔键；缺行/非法 → `None`（调用方决定回落到 env 默认）。"""
        raw = await self.get(key)
        if raw is None:
            return None
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        return None

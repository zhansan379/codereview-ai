"""后台 REST 鉴权依赖（DESIGN §14.3）。

`get_current_user` 解析 Bearer JWT（HS256，`secret_key` 来自 Settings），
失败一律 401；返回 `sub`（单用户 = "admin"）。webhook 路由不经过这里——webhook
靠签名鉴权（DESIGN §9），后台 REST 才需要 JWT。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.storage.db import session_factory

_bearer = HTTPBearer(auto_error=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """每请求一个会话；引擎来自 `app.state.engine`（生命周期已建）。"""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "数据库未就绪")
    async with session_factory(engine)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """从 Bearer 里解析并校验 JWT，返回登录用户 `sub`；缺失/无效/过期 → 401。"""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未提供 Bearer token")
    token = credentials.credentials
    settings = getattr(request.app.state, "settings", None)
    secret = getattr(settings, "secret_key", "") or ""
    if not secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "服务端密钥未配置")
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 无效或已过期") from exc
    return str(payload.get("sub", ""))

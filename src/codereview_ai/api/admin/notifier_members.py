"""系统级 @成员名单 CRUD（一行 = 一个真人，含跨平台 ID 别名）。

成员表与具体渠道无关（属系统级）；渠道 × 成员的绑定在 `notifiers.py` 里用
`NotifierRouteMember` 维护。`git_username` 是 forge 提交用户名，供「提交者 @」
按渠道解析成各平台认识的 ID；三平台字段各自为空就表示该平台无标识（推送跳过）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from codereview_ai.api.deps import get_db, require_permission
from codereview_ai.storage.models import NotifierMember, NotifierRouteMember

# IM 成员目录含手机号/企微/飞书 ID 等敏感数据，增删改（含读）须 `notifiers:manage`，
# 不能仅登录即可。require_permission 已含登录鉴权。
router = APIRouter(prefix="/notifiers/members", dependencies=[
    Depends(require_permission("notifiers:manage")),
])


class MemberOut(BaseModel):
    id: int
    name: str
    git_username: str
    dingtalk_mobile: str
    wecom_userid: str
    feishu_open_id: str


class MemberWrite(BaseModel):
    name: str = ""
    git_username: str = ""
    dingtalk_mobile: str = ""
    wecom_userid: str = ""
    feishu_open_id: str = ""


def _to_out(row: NotifierMember) -> MemberOut:
    return MemberOut(
        id=row.id,
        name=row.name,
        git_username=row.git_username,
        dingtalk_mobile=row.dingtalk_mobile,
        wecom_userid=row.wecom_userid,
        feishu_open_id=row.feishu_open_id,
    )


async def _get_or_404(session: AsyncSession, member_id: int) -> NotifierMember:
    row = (await session.execute(
        select(NotifierMember).where(NotifierMember.id == member_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "成员不存在")
    return row


@router.get("", response_model=list[MemberOut])
async def list_members(session: AsyncSession = Depends(get_db)) -> list[MemberOut]:
    rows = (await session.execute(
        select(NotifierMember).order_by(NotifierMember.id)
    )).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def create_member(
    body: MemberWrite, session: AsyncSession = Depends(get_db)
) -> MemberOut:
    row = NotifierMember(
        name=body.name,
        git_username=body.git_username,
        dingtalk_mobile=body.dingtalk_mobile,
        wecom_userid=body.wecom_userid,
        feishu_open_id=body.feishu_open_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.put("/{member_id}", response_model=MemberOut)
async def update_member(
    member_id: int, body: MemberWrite, session: AsyncSession = Depends(get_db)
) -> MemberOut:
    row = await _get_or_404(session, member_id)
    for field in ("name", "git_username", "dingtalk_mobile", "wecom_userid", "feishu_open_id"):
        setattr(row, field, getattr(body, field))
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(member_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_or_404(session, member_id)
    await session.execute(
        delete(NotifierRouteMember).where(NotifierRouteMember.member_id == member_id)
    )
    await session.delete(row)
    await session.commit()
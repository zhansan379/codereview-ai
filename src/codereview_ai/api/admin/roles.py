"""角色管理 REST（F5.11）：增删改角色、分配权限。`roles:manage`/`users:manage` 可读。

内置角色（`is_system`）结构不可改、不可删；自定义角色可勾选任意权限组合但**不可设
`is_super`**（超管仅 admin 内置角色保留）。`GET /roles/permissions` 暴露权限目录供
前端勾选。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette import status

from codereview_ai.api.deps import (
    get_current_user,
    get_db,
    require_any_permission,
)
from codereview_ai.storage.models import Permission, Role, RolePermission
from codereview_ai.storage.seed import (
    PERMISSION_CATALOG,
    PERMISSION_TREE,
    PermissionDef,
    PermissionGroup,
    PermissionNode,
    with_implied_view,
)

router = APIRouter(
    prefix="/roles",
    dependencies=[Depends(get_current_user),
                  Depends(require_any_permission(
                      "roles:view", "roles:edit", "users:view", "users:edit"))],
)


class PermissionNodeOut(BaseModel):
    """权限树节点：分组节点 `code` 为空、只带 `children`；叶子节点带 `code`/`scope`。"""

    key: str
    name: str
    code: str | None = None
    scope: str | None = None
    description: str = ""
    children: list[PermissionNodeOut] = []


PermissionNodeOut.model_rebuild()


def _perm_tree(nodes: tuple[PermissionNode, ...]) -> list[PermissionNodeOut]:
    out: list[PermissionNodeOut] = []
    for node in nodes:
        if isinstance(node, PermissionGroup):
            out.append(PermissionNodeOut(
                key=node.key, name=node.name, children=_perm_tree(node.children)))
        elif isinstance(node, PermissionDef):
            out.append(PermissionNodeOut(
                key=node.code, name=node.name, code=node.code,
                scope=node.scope, description=node.description))
    return out


class RoleOut(BaseModel):
    id: int
    name: str
    description: str = ""
    is_super: bool
    is_system: bool
    all_projects: bool
    builtin_code: str = ""
    permissions: list[str]
    member_count: int = 0


class RoleCreate(BaseModel):
    name: str
    description: str = ""
    all_projects: bool = False
    permission_codes: list[str] = []


class RoleUpdate(BaseModel):
    description: str | None = None
    all_projects: bool | None = None


class RolePermissionsWrite(BaseModel):
    permission_codes: list[str] = []


async def _resolve_perms(session: AsyncSession, codes: set[str]) -> list[Permission]:
    """把请求的权限码解析成 Permission 行；目录里有、DB 表里缺的行即时补建（自愈）。

    角色分配按 `Permission.code` 匹配 DB 行，存量库若缺某目录行的码会被静默丢弃（表现为
    「勾上刷新又没了」，如新加的 caches:manage）。这里对在目录里但无行的码即时建行，保证
    分配持久。
    """
    rows: dict[str, Permission] = {
        p.code: p for p in (await session.execute(
            select(Permission).where(Permission.code.in_(codes))
        )).scalars()
    }
    catalog = {c: (n, s, d) for c, n, s, d in PERMISSION_CATALOG}
    for code in codes:
        if code not in rows and code in catalog:
            n, s, d = catalog[code]
            p = Permission(code=code, name=n, scope=s, description=d, is_system=True)
            session.add(p)
            rows[code] = p
    # 新补的行留待 commit 时按表依赖顺序自动 flush（先 Permission 后 junction）
    return list(rows.values())


def _to_out(row: Role, member_count: int) -> RoleOut:
    return RoleOut(
        id=row.id, name=row.name, description=row.description,
        is_super=row.is_super, is_system=row.is_system, all_projects=row.all_projects,
        builtin_code=row.builtin_code,
        permissions=sorted({p.code for p in row.permissions}),
        member_count=member_count,
    )


async def _get_role(session: AsyncSession, role_id: int) -> Role:
    row = (await session.execute(
        select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "角色不存在")
    return row


async def _member_count(session: AsyncSession, role_id: int) -> int:
    from codereview_ai.storage.models import User
    return int((await session.execute(
        select(func.count()).select_from(User).where(User.role_id == role_id)
    )).scalar_one() or 0)


@router.get("/permissions", response_model=list[PermissionNodeOut])
async def list_permissions() -> list[PermissionNodeOut]:
    """返回权限模块树（PERMISSION_TREE 单一事实源），前端按层级勾选用。"""
    return _perm_tree(PERMISSION_TREE)


@router.get("", response_model=list[RoleOut])
async def list_roles(session: AsyncSession = Depends(get_db)) -> list[RoleOut]:
    rows = (await session.execute(
        select(Role).order_by(Role.id).options(selectinload(Role.permissions))
    )).scalars().all()
    return [_to_out(r, await _member_count(session, r.id)) for r in rows]


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_any_permission("roles:edit"))])
async def create_role(body: RoleCreate, session: AsyncSession = Depends(get_db)) -> RoleOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "角色名不能为空")
    role = Role(name=name, description=body.description,
                is_super=False, all_projects=body.all_projects, is_system=False)
    session.add(role)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "角色名已存在") from None
    await session.refresh(role)
    if body.permission_codes:
        perms = await _resolve_perms(session, with_implied_view(set(body.permission_codes)))
        for p in perms:
            session.add(RolePermission(role_id=role.id, permission_id=p.id))
        await session.commit()
    role = await _get_role(session, role.id)
    return _to_out(role, await _member_count(session, role.id))


@router.put("/{role_id}", response_model=RoleOut,
            dependencies=[Depends(require_any_permission("roles:edit"))])
async def update_role(
    role_id: int, body: RoleUpdate, session: AsyncSession = Depends(get_db)
) -> RoleOut:
    row = await _get_role(session, role_id)
    if row.is_system:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置角色不可修改结构")
    if body.description is not None:
        row.description = body.description
    if body.all_projects is not None and not row.is_super:
        row.all_projects = body.all_projects
    await session.commit()
    return _to_out(row, await _member_count(session, role_id))


@router.put("/{role_id}/permissions", response_model=RoleOut,
            dependencies=[Depends(require_any_permission("roles:edit"))])
async def set_role_permissions(
    role_id: int, body: RolePermissionsWrite, session: AsyncSession = Depends(get_db)
) -> RoleOut:
    """全量替换角色权限（`permission_codes`）。内置角色可改权限（保留删结构限制）。
    成对授予：`:edit` 自动补上成对的 `:view`。"""
    row = await _get_role(session, role_id)
    # 全量替换：走 relationship 保持内存集合一致（避免 bulk delete 后读旧关系）
    perms = await _resolve_perms(session, with_implied_view(set(body.permission_codes)))
    row.permissions.clear()
    row.permissions = list(perms)
    await session.commit()
    return _to_out(row, await _member_count(session, role_id))


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_any_permission("roles:edit"))])
async def delete_role(role_id: int, session: AsyncSession = Depends(get_db)) -> None:
    row = await _get_role(session, role_id)
    if row.is_system:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置角色不可删除")
    if await _member_count(session, role_id) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "还有用户引用该角色，无法删除")
    await session.delete(row)
    await session.commit()

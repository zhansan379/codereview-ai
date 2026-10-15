"""RBAC 引导种子（F5.11）。幂等：已有任何 user 即整体跳过。

顺序（FK 依赖）：permissions → roles → role_permission → 首个 admin 用户。
`PERMISSION_CATALOG` 是权限的单一事实源；`DEFAULT_ROLES` 定义四套内置角色。
首个 admin 的密码派生自 `CR_ADMIN_PASSWORD`（启动已 fail-fast 必非空）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.security import hash_password
from codereview_ai.storage.models import Permission, Role, RolePermission, User

logger = logging.getLogger("codereview_ai.seed")


@dataclass(frozen=True)
class PermissionDef:
    """叶子权限项（可勾选的操作/按钮）。`scope` 仅作后端授权用，不在前端树里展示。"""

    code: str
    name: str
    scope: str
    description: str = ""


@dataclass(frozen=True)
class PermissionGroup:
    """权限分组节点（模块/子模块），仅用于前端树层级展示，不承载权限码。"""

    key: str
    name: str
    children: tuple[PermissionNode, ...]


PermissionNode = PermissionDef | PermissionGroup


#: 权限模块树（单一事实源）：三级结构——一级=菜单分组（分析/审查/资源/系统），二级=菜单项
#: （与左侧菜单同名），三级=具体权限（查看/编辑，及按钮级如手动补拉/平台配置）。可动态嵌套。
#: `scope` 仅用于后端项目级隔离判定（global/project），前端树不显示 scope。
PERMISSION_TREE: tuple[PermissionNode, ...] = (
    PermissionGroup("analytics", "分析", (
        PermissionGroup("workrate", "提交分析", (
            PermissionDef("workrate:view", "查看", "global", "查看提交分析"),
        )),
        PermissionGroup("members", "成员分析", (
            PermissionDef("members:view", "查看", "global", "查看成员分析"),
        )),
    )),
    PermissionGroup("review", "审查", (
        PermissionGroup("projects", "项目", (
            PermissionDef("projects:view", "查看", "project", "查看项目及其配置"),
            PermissionDef("projects:edit", "编辑", "project", "创建/修改/删除项目、成员"),
            PermissionDef("pulls:edit", "手动补拉", "global", "触发主动补拉 PR/MR"),
        )),
        PermissionGroup("reviews", "审查记录", (
            PermissionDef("reviews:view", "查看", "project", "查看审查列表/详情/对话"),
            PermissionDef("reviews:edit", "编辑", "project", "删除审查记录、手动重试"),
        )),
        PermissionGroup("schedules", "定时任务", (
            PermissionDef("schedules:view", "查看", "global", "查看调度任务"),
            PermissionDef("schedules:edit", "编辑", "global", "新增/修改/删除/立即执行任务"),
        )),
    )),
    PermissionGroup("resource", "资源", (
        PermissionGroup("models", "模型", (
            PermissionDef("models:view", "查看", "global", "查看模型配置"),
            PermissionDef("models:edit", "编辑", "global", "新增/修改/删除模型与 API Key"),
        )),
        PermissionGroup("notifiers", "IM 通知", (
            PermissionDef("notifiers:view", "查看", "global", "查看通知渠道与路由"),
            PermissionDef("notifiers:edit", "编辑", "global", "增删改渠道/路由、成员名单"),
        )),
        PermissionGroup("caches", "拉取缓存", (
            PermissionDef("caches:view", "查看", "global", "查看本地拉取缓存与清除策略"),
            PermissionDef("caches:edit", "编辑", "global", "删除缓存、重建、执行清除策略"),
        )),
    )),
    PermissionGroup("system", "系统", (
        PermissionGroup("settings", "设置", (
            PermissionDef("settings:view", "查看", "global", "查看运行时设置"),
            PermissionDef("settings:edit", "编辑", "global", "并发/双轨默认/补拉范围/静态分析"),
            PermissionDef("forges:edit", "平台配置", "global", "配置 GitHub/GitLab 接入与密钥"),
        )),
        PermissionGroup("users", "用户", (
            PermissionDef("users:view", "查看", "global", "查看用户列表"),
            PermissionDef("users:edit", "编辑", "global", "增删改用户、重置密码"),
        )),
        PermissionGroup("roles", "角色", (
            PermissionDef("roles:view", "查看", "global", "查看角色列表"),
            PermissionDef("roles:edit", "编辑", "global", "增删改角色与权限分配"),
        )),
    )),
)


def _iter_defs(nodes: tuple[PermissionNode, ...]) -> Iterator[PermissionDef]:
    for node in nodes:
        if isinstance(node, PermissionGroup):
            yield from _iter_defs(node.children)
        else:
            yield node


#: 扁平权限目录（由 PERMISSION_TREE 派生）：(code, name, scope, description)。
#: 保留扁平视图供后端授权/对账/落库使用，避免各调用点都去遍历树。
PERMISSION_CATALOG: list[tuple[str, str, str, str]] = [
    (d.code, d.name, d.scope, d.description) for d in _iter_defs(PERMISSION_TREE)
]

#: 目录内全部合法权限码
PERMISSION_CODES: frozenset[str] = frozenset(c for c, _n, _s, _d in PERMISSION_CATALOG)

#: `:edit` → 成对的 `:view` 码；仅当该 view 码在目录内时才算成对（可成对的模块）
EDIT_VIEW_PAIRS: dict[str, str] = {
    code: code[: -len(":edit")] + ":view"
    for code in PERMISSION_CODES
    if code.endswith(":edit") and code[: -len(":edit")] + ":view" in PERMISSION_CODES
}


def with_implied_view(codes: set[str]) -> set[str]:
    """成对授予：`:edit` 自动补上对应的 `:view`（若目录中存在成对 view）。"""
    out = set(codes)
    for code in list(out):
        view = EDIT_VIEW_PAIRS.get(code)
        if view is not None:
            out.add(view)
    return out

#: code → (name, is_super, all_projects, [perm_codes])。is_super 仅 admin 预留。
DEFAULT_ROLES: dict[str, tuple[str, bool, bool, list[str]]] = {
    "admin": ("管理员", True, True, [c for c, _, _, _ in PERMISSION_CATALOG]),
    "tech_lead": ("技术负责人", False, True, [
        "projects:view", "projects:edit", "pulls:edit", "reviews:view", "reviews:edit",
        "caches:view", "caches:edit", "workrate:view", "members:view",
    ]),
    "developer": ("开发", False, False, [
        "projects:view", "reviews:view", "workrate:view", "members:view",
    ]),
    "viewer": ("观察者", False, False, [
        "projects:view", "reviews:view", "workrate:view", "members:view",
    ]),
}


async def seed_rbac(session: AsyncSession, admin_password: str) -> list[str]:
    """幂等播种 RBAC。返回本次实际新建的角色 code 列表（便于日志）。

    若已有任何用户（升级部署）则整体跳过，避免覆盖/重复。
    """
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if count > 0:
        logger.info("用户表非空，跳过 RBAC 种子（已有 %s 个用户）", count)
        return []

    created: list[str] = []

    # 1) permissions
    perm_by_code: dict[str, Permission] = {}
    for code, name, scope, desc in PERMISSION_CATALOG:
        prow = (await session.execute(
            select(Permission).where(Permission.code == code)
        )).scalar_one_or_none()
        if prow is None:
            prow = Permission(code=code, name=name, scope=scope, description=desc, is_system=True)
            session.add(prow)
        perm_by_code[code] = prow
    await session.flush()

    # 2) roles
    role_by_code: dict[str, Role] = {}
    for code, (name, is_super, all_projects, _perm_codes) in DEFAULT_ROLES.items():
        rrow = (await session.execute(
            select(Role).where(Role.builtin_code == code)
        )).scalar_one_or_none()
        if rrow is None:
            rrow = Role(
                name=name, description=name, is_super=is_super, is_system=True,
                all_projects=all_projects, builtin_code=code,
            )
            session.add(rrow)
            created.append(code)
        role_by_code[code] = rrow
    await session.flush()

    # 3) role-permission junction
    for code, (_, _, _, perm_codes) in DEFAULT_ROLES.items():
        role = role_by_code[code]
        for pcode in perm_codes:
            pid = perm_by_code[pcode].id
            existing = (await session.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role.id, RolePermission.permission_id == pid
                )
            )).scalar_one_or_none()
            if existing is None:
                session.add(RolePermission(role_id=role.id, permission_id=pid))

    # 4) 首个 admin 用户
    admin_role = role_by_code["admin"]
    session.add(User(username="admin", password_hash=hash_password(admin_password),
                     display_name="管理员", enabled=True, role_id=admin_role.id))

    await session.commit()
    logger.info("RBAC 种子完成，新建角色：%s", created or ["（复用内置）"])
    return created


async def sync_default_role_permissions(session: AsyncSession) -> int:
    """对账内置角色权限与 DEFAULT_ROLES（幂等，每次启动调用）。

    `seed_rbac` 只在空库播种；存量库随版本演进给内置角色加新权限码（如 developer 的
    stats:view、tech_lead 的 caches:manage）时，role_permission 关联不会自动跟上，表现为
    「升级部署的内置角色缺新权限，得去角色页手动勾」。这里按 DEFAULT_ROLES 给每个内置
    角色补缺的关联行——**只增不删**：管理员在角色页给内置角色的手工加权保留，手工收回的
    默认权限会在下次启动补回（内置角色以目录为准的语义）。自定义角色（无 builtin_code）
    不受影响。须在 `sync_permission_catalog` 之后调用（先有 Permission 行）。返回补加条数。
    """
    added = 0
    for code, (*_meta, perm_codes) in DEFAULT_ROLES.items():
        role = (await session.execute(
            select(Role).where(Role.builtin_code == code)
        )).scalar_one_or_none()
        if role is None:
            continue
        perm_ids = set((await session.execute(
            select(Permission.id).where(Permission.code.in_(perm_codes))
        )).scalars())
        existing = set((await session.execute(
            select(RolePermission.permission_id)
            .where(RolePermission.role_id == role.id)
        )).scalars())
        for pid in perm_ids - existing:
            session.add(RolePermission(role_id=role.id, permission_id=pid))
            added += 1
    if added:
        await session.commit()
        logger.info("内置角色权限对账补加 %d 条", added)
    return added


async def prune_obsolete_permissions(session: AsyncSession) -> int:
    """清理不在权限目录里、已失效的权限（幂等，每次启动调用）。

    目录删掉某个权限码（如 tasks:manage / reviews:update）后，seed 只在空库播种、不会覆盖
    存量库；旧的角色仍可能残留指向旧 Permission 行的 role_permission 关联与孤儿的 Permission 行。
    这里把两者一并清除，返回清理条数。新库无残留时是纯 no-op。
    """
    valid = {c for c, _n, _s, _d in PERMISSION_CATALOG}
    obsolete = list((await session.execute(
        select(Permission).where(Permission.code.notin_(valid))
    )).scalars().all())
    if not obsolete:
        return 0
    obsolete_ids = [p.id for p in obsolete]
    # 先清 junction 避免外键残留，再删权限行
    await session.execute(
        delete(RolePermission).where(RolePermission.permission_id.in_(obsolete_ids))
    )
    for p in obsolete:
        await session.delete(p)
    await session.commit()
    logger.info("清理失效权限 %d 条：%s",
                len(obsolete), ", ".join(p.code for p in obsolete))
    return len(obsolete)


async def sync_permission_catalog(session: AsyncSession) -> int:
    """对账权限目录与 permission 表（幂等，每次启动调用）。

    `seed_rbac` 只在空库播种，存量库新增某个权限码（如 caches:manage）时 Permission 行不会自动
    出现；角色分配按 permission_codes 去匹配 DB 行（`set_role_permissions`），匹配不到就静默丢弃，
    表现为「勾上刷新又没了」。此函数把目录里有、表里缺的行补上，返回新建条数。新库无缺时 no-op。
    与 `prune_obsolete_permissions` 一增一删，共同把 DB 对账到与目录一致。
    """
    existing = {p.code for p in (await session.execute(select(Permission))).scalars()}
    added: list[str] = []
    for code, name, scope, desc in PERMISSION_CATALOG:
        if code not in existing:
            session.add(Permission(code=code, name=name, scope=scope,
                                   description=desc, is_system=True))
            added.append(code)
    if added:
        await session.commit()
        logger.info("补齐权限目录 %d 条：%s", len(added), ", ".join(added))
    return len(added)

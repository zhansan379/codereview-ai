"""RBAC 引导种子（F5.11）。幂等：已有任何 user 即整体跳过。

顺序（FK 依赖）：permissions → roles → role_permission → 首个 admin 用户。
`PERMISSION_CATALOG` 是权限的单一事实源；`DEFAULT_ROLES` 定义四套内置角色。
首个 admin 的密码派生自 `CR_ADMIN_PASSWORD`（启动已 fail-fast 必非空）。
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.security import hash_password
from codereview_ai.storage.models import Permission, Project, Role, RolePermission, User, Workspace

logger = logging.getLogger("codereview_ai.seed")

#: (code, name, scope, description) —— 权限目录单一事实源
PERMISSION_CATALOG: list[tuple[str, str, str, str]] = [
    # —— 全局（系统级管理）——
    ("settings:manage", "运行时设置", "global", "并发数等全局运行时设置"),
    ("stats:view", "统计看板", "global", "查看统计看板"),
    ("forges:manage", "平台配置", "global", "GitHub/GitLab 接入与密钥"),
    ("models:manage", "模型配置", "global", "LLM 模型与 API Key"),
    ("notifiers:manage", "IM 通知配置", "global", "钉钉/飞书/企微渠道与路由"),
    ("schedules:manage", "定时任务", "global", "补拉/日报调度任务"),
    ("users:manage", "用户管理", "global", "增删改用户、重置密码"),
    ("roles:manage", "角色管理", "global", "增删改角色与权限分配"),
    ("pulls:manage", "手动补拉", "global", "触发主动补拉 PR/MR"),
    ("caches:manage", "拉取缓存管理", "global", "查看/删除本地拉取缓存与清除策略"),
    # —— 项目（经成员关系/全项目角色生效）——
    ("projects:view", "查看项目", "project", "查看项目及其配置"),
    ("projects:manage", "管理项目", "project", "创建/修改/删除项目、成员"),
    ("reviews:view", "查看审查记录", "project", "查看审查列表/详情/对话"),
    ("reviews:manage", "管理审查记录", "project", "删除审查记录、手动重试"),
]

#: code → (name, is_super, all_projects, [perm_codes])。is_super 仅 admin 预留。
DEFAULT_ROLES: dict[str, tuple[str, bool, bool, list[str]]] = {
    "admin": ("管理员", True, True, [c for c, _, _, _ in PERMISSION_CATALOG]),
    "tech_lead": ("技术负责人", False, True, [
        "projects:view", "projects:manage", "reviews:view", "reviews:manage",
        "pulls:manage", "caches:manage", "stats:view",
    ]),
    "developer": ("开发", False, False, [
        "projects:view", "reviews:view",
    ]),
    "viewer": ("观察者", False, False, [
        "projects:view", "reviews:view", "stats:view",
    ]),
    # 自助注册默认角色：只含项目作用域码（在自己的 workspace 内建/管项目+看审查），
    # 不含任何全局码（settings/forges/models/users/roles/schedules/stats 一律不给）。
    "member": ("成员", False, False, [
        "projects:view", "projects:manage", "reviews:view", "reviews:manage",
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


async def ensure_member_role(session: AsyncSession) -> bool:
    """确保自助注册默认角色 `member` 存在（幂等）。

    `seed_rbac` 只在空库播种（已有用户即跳过）；存量升级库不会自带 member 角色，
    而注册端点依赖它。这里在目录/权限已对账的基础上，补建缺失的 member 角色及其
    权限关联（只增不删，管理员后续手动加的角色权限不动）。
    """
    role = (await session.execute(
        select(Role).where(Role.builtin_code == "member")
    )).scalar_one_or_none()
    name, _is_super, _all_projects, perm_codes = DEFAULT_ROLES["member"]
    created = role is None
    if role is None:
        role = Role(
            name=name, description=name, is_super=False, is_system=True,
            all_projects=False, builtin_code="member",
        )
        session.add(role)
        await session.flush()
    # 只补缺的 junction 行（权限行由 sync_permission_catalog 保证存在）
    perms = {p.id for p in (await session.execute(
        select(Permission).where(Permission.code.in_(perm_codes))
    )).scalars().all()}
    have = {rp.permission_id for rp in (await session.execute(
        select(RolePermission).where(RolePermission.role_id == role.id)
    )).scalars().all()}
    missing = sorted(perms - have)
    for pid in missing:
        session.add(RolePermission(role_id=role.id, permission_id=pid))
    await session.commit()
    if created or missing:
        logger.info("member 角色就绪（新增权限关联 %d 条：%s）",
                    len(missing), missing)
    return created or bool(missing)


async def ensure_workspace_backfill(session: AsyncSession) -> bool:
    """存量数据回填默认工作区（幂等）：建「默认工作区」并把无归属项目挂到其下。

    只在「一张 workspace 都没有」时执行一次（对齐 `seed_rbac` 的整体跳过闸）。
    返回 `True` 当本次创建了默认工作区。owner 取首个用户（播种的 admin）。
    """
    count = (await session.execute(select(func.count()).select_from(Workspace))).scalar_one()
    if count > 0:
        return False
    owner_id = (await session.execute(
        select(User.id).order_by(User.id).limit(1)
    )).scalar_one_or_none()
    ws = Workspace(name="默认工作区", slug="default", owner_id=owner_id)
    session.add(ws)
    await session.flush()
    await session.execute(
        update(Project).where(Project.workspace_id.is_(None)).values(workspace_id=ws.id)
    )
    await session.commit()
    logger.info("工作区回填：创建默认工作区 id=%s（owner=%s），存量项目归入其下", ws.id, owner_id)
    return True


async def user_owned_workspace_ids(session: AsyncSession, user: User) -> tuple[bool, set[int]]:
    """用户拥有的 workspace 集。

    `(is_all, ids)`：超管 is_all=True（全量直通）；否则 ids=其 owner 的空间。
    """
    if user.role and user.role.is_super:
        return True, set()
    rows = (await session.execute(
        select(Workspace.id).where(Workspace.owner_id == user.id)
    )).scalars().all()
    return False, set(rows)


async def user_workspace(session: AsyncSession, user: User) -> Workspace | None:
    """用户要建项目时归属的 workspace：超管取默认/管理端空间；否则取其私有空间。"""
    if user.role and user.role.is_super:
        return (await session.execute(
            select(Workspace).order_by(Workspace.id).limit(1)
        )).scalar_one_or_none()
    return (await session.execute(
        select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.id).limit(1)
    )).scalar_one_or_none()

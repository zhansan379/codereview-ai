"""RBAC 引导种子（F5.11）。幂等：已有任何 user 即整体跳过。

顺序（FK 依赖）：permissions → roles → role_permission → 首个 admin 用户。
`PERMISSION_CATALOG` 是权限的单一事实源；`DEFAULT_ROLES` 定义四套内置角色。
首个 admin 的密码派生自 `CR_ADMIN_PASSWORD`（启动已 fail-fast 必非空）。
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.security import hash_password
from codereview_ai.storage.models import Permission, Role, RolePermission, User

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
    ("tasks:manage", "任务监控", "global", "任务列表与手动重试"),
    ("users:manage", "用户管理", "global", "增删改用户、重置密码"),
    ("roles:manage", "角色管理", "global", "增删改角色与权限分配"),
    ("pulls:manage", "手动补拉", "global", "触发主动补拉 PR/MR"),
    # —— 项目（经成员关系/全项目角色生效）——
    ("projects:view", "查看项目", "project", "查看项目及其配置"),
    ("projects:manage", "管理项目", "project", "创建/修改/删除项目、成员"),
    ("reviews:view", "查看审查记录", "project", "查看审查列表/详情/对话"),
    ("reviews:manage", "管理审查记录", "project", "删除审查记录、手动重试"),
    ("reviews:update", "更新审查意见", "project", "人工更新 finding 状态（waived/active）"),
]

#: code → (name, is_super, all_projects, [perm_codes])。is_super 仅 admin 预留。
DEFAULT_ROLES: dict[str, tuple[str, bool, bool, list[str]]] = {
    "admin": ("管理员", True, True, [c for c, _, _, _ in PERMISSION_CATALOG]),
    "tech_lead": ("技术负责人", False, True, [
        "projects:view", "projects:manage", "reviews:view", "reviews:manage",
        "reviews:update", "pulls:manage", "stats:view",
    ]),
    "developer": ("开发", False, False, [
        "projects:view", "reviews:view", "reviews:update",
    ]),
    "viewer": ("观察者", False, False, [
        "projects:view", "reviews:view", "stats:view",
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

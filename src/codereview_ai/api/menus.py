"""侧边菜单接口（F5.11 RBAC 延伸）。

`MENU_CATALOG` 是菜单的单一事实源：每个菜单项 `name` 与前端路由 `name` 一一对应，
前端据此把菜单渲染成侧栏、并按 `names` 动态挂载可访问路由。

权限为 `None` 的项对所有登录用户可见（如仪表盘）；`HIDDEN_ROUTES` 是不出现在菜单、
但需要动态挂载的路由（如审查详情），其可访问性由权限码决定。响应里的 `names` 是
「菜单项 + 隐藏路由」中当前用户可访问的路由 name 合集，供前端筛选路由。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from codereview_ai.api.deps import CurrentUser, resolved_permission_codes

router = APIRouter()

#: (group_key, group_title_key, [(name, path, title_key, icon, permission|None)])
MENU_CATALOG: list[tuple[str, str, list[tuple[str, str, str, str, str | None]]]] = [
    ("analytics", "menu.groupAnalytics", [
        ("Dashboard", "/dashboard", "menu.dashboard", "DataBoard", None),
        ("Workrate", "/workrate", "menu.workrate", "DataAnalysis", "workrate:view"),
        ("Members", "/members", "menu.members", "User", "members:view"),
    ]),
    ("review", "menu.groupReview", [
        ("Projects", "/projects", "menu.projects", "Folder", "projects:view"),
        ("Reviews", "/reviews", "menu.reviews", "Document", "reviews:view"),
        ("Schedules", "/schedules", "menu.schedules", "Timer", "schedules:view"),
    ]),
    ("resource", "menu.groupResource", [
        ("Models", "/models", "menu.models", "Cpu", "models:view"),
        ("Notifiers", "/notifiers", "menu.notifiers", "Bell", "notifiers:view"),
        ("Caches", "/clone-caches", "menu.caches", "Box", "caches:view"),
    ]),
    ("system", "menu.groupSystem", [
        ("Settings", "/settings", "menu.settings", "Setting", "settings:view"),
        ("Users", "/users", "menu.users", "User", "users:view"),
        ("Roles", "/roles", "menu.roles", "Key", "roles:view"),
    ]),
]

#: (route_name, permission|None) —— 不出现在菜单、但需按权限动态挂载的前端路由
HIDDEN_ROUTES: list[tuple[str, str | None]] = [
    ("ReviewDetail", "reviews:view"),
    ("ReviewConversation", "reviews:view"),
]


class MenuItem(BaseModel):
    name: str
    path: str
    title_key: str
    icon: str | None = None
    permission: str | None = None


class MenuGroup(BaseModel):
    key: str
    title_key: str
    items: list[MenuItem]


class MenuResponse(BaseModel):
    #: 侧栏菜单树（按用户权限过滤，空组剔除）
    groups: list[MenuGroup]
    #: 可访问路由 name 合集（菜单项 + 隐藏路由），供前端筛选并动态挂载路由
    names: list[str]


@router.get("/menus", response_model=MenuResponse)
async def list_menus(user: CurrentUser) -> MenuResponse:
    """返回当前用户可见的菜单树与可访问路由 name 列表。"""
    codes = resolved_permission_codes(user)
    groups: list[MenuGroup] = []
    names: list[str] = []
    for key, group_title_key, items in MENU_CATALOG:
        visible: list[MenuItem] = []
        for name, path, item_title_key, icon, perm in items:
            if perm is None or perm in codes:
                visible.append(MenuItem(
                    name=name, path=path, title_key=item_title_key,
                    icon=icon, permission=perm,
                ))
        if visible:
            groups.append(MenuGroup(key=key, title_key=group_title_key, items=visible))
            names.extend(it.name for it in visible)
    for route_name, perm in HIDDEN_ROUTES:
        if perm is None or perm in codes:
            names.append(route_name)
    return MenuResponse(groups=groups, names=names)

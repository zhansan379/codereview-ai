"""租户成本聚合（多租户按租户计费）：`GET /api/usage`（JWT 鉴权）。

把 `model_usage` 按 **workspace → project** 分桶聚合成本/token/请求数（计费底座）。
`model_usage` 未直接存 workspace_id/project_id，经 `task_id → review_task.project_id →
project.workspace_id` 回溯归因（零 schema 迁移、既有数据即用）。不可归因的用量
（task_id 为 NULL 或项目已删）不进入报表。

- **超管 / 全项目角色**：全量，可选 `workspace_id` 收敛到单空间；
- **非超管**：限定在**自己拥有(owner)**的 workspace 内（多租户自助计费页）——每用户只能看到
  自家空间的成本，不验 `stats:view`（member 角色也有权看自家账单）。
- 可选 `days` 只统计近 N 天（`ModelUsage.ts` 过滤），默认全期。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from codereview_ai.api.deps import CurrentUser, get_current_user, get_db
from codereview_ai.storage.models import ModelUsage, Project, ReviewTask, Workspace
from codereview_ai.storage.seed import user_owned_workspace_ids

#: 租户自助成本页：任何登录用户可见（memeber 角色也有权看自家账单），数据层按 workspace 隔离。
router = APIRouter(prefix="/usage", dependencies=[Depends(get_current_user)])


class ProjectUsageItem(BaseModel):
    project_id: int
    project_name: str
    workspace_id: int | None
    requests: int
    total_tokens: int
    cost: float


class WorkspaceUsageItem(BaseModel):
    workspace_id: int | None
    workspace_name: str
    projects: list[ProjectUsageItem]
    requests: int
    total_tokens: int
    cost: float


class UsageSummary(BaseModel):
    workspaces: list[WorkspaceUsageItem]
    requests: int
    total_tokens: int
    cost: float


@router.get("", response_model=UsageSummary)
async def usage_summary(
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
    days: int = 0,
    workspace_id: int | None = None,
) -> UsageSummary:
    """按 workspace/project 聚合成本（多租户计费）：超管全量、非超管自有空间。"""
    is_all, ws_ids = await user_owned_workspace_ids(session, user)
    if not is_all:
        if not ws_ids:
            raise HTTPException(403, "您没有可统计的工作区")
        if workspace_id is not None and workspace_id not in ws_ids:
            raise HTTPException(404, "工作区不存在或无权访问")

    start: datetime | None = None
    if days and days > 0:
        # SQLite 读回 `DateTime(timezone=True)` 为 naive（本地墙钟即 UTC）→ 用 naive 起点比对
        start = (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None)

    conds: list[Any] = [ModelUsage.task_id.isnot(None), ReviewTask.project_id.isnot(None)]
    if start is not None:
        conds.append(ModelUsage.ts >= start)
    if not is_all:
        conds.append(Project.workspace_id.in_(list(ws_ids)))
    elif workspace_id is not None:
        conds.append(Project.workspace_id == workspace_id)

    rows = (await session.execute(
        select(
            ReviewTask.project_id,
            Project.workspace_id,
            func.count(),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cost), 0),
        )
        .join(ReviewTask, ReviewTask.id == ModelUsage.task_id)
        .join(Project, Project.id == ReviewTask.project_id)
        .where(*conds)
        .group_by(ReviewTask.project_id, Project.workspace_id)
    )).all()

    proj_ids = [r[0] for r in rows]
    ws_ids_in_rows = {r[1] for r in rows}

    # 项目显示名：repo_full_name，缺失回退 provider/repo_id
    project_names: dict[int, str] = {}
    if proj_ids:
        proj_rows = (await session.execute(
            select(Project.id, Project.provider, Project.repo_id, Project.repo_full_name)
            .where(Project.id.in_(proj_ids))
        )).all()
        project_names = {
            pid: (full or f"{provider}/{repo_id}")
            for pid, provider, repo_id, full in proj_rows
        }

    ws_names: dict[int, str] = {}
    ws_to_fetch = [w for w in ws_ids_in_rows if w is not None]
    if ws_to_fetch:
        ws_rows = (await session.execute(
            select(Workspace.id, Workspace.slug).where(Workspace.id.in_(ws_to_fetch))
        )).all()
        ws_names = {wid: slug for wid, slug in ws_rows}

    by_ws: dict[int | None, list[ProjectUsageItem]] = {}
    totals = {"requests": 0, "tokens": 0, "cost": 0.0}
    for pid, wid, reqs, tokens, cost in rows:
        item = ProjectUsageItem(
            project_id=pid,
            project_name=project_names.get(pid, f"项目#{pid}"),
            workspace_id=wid,
            requests=int(reqs),
            total_tokens=int(tokens),
            cost=round(float(cost or 0), 4),
        )
        by_ws.setdefault(wid, []).append(item)
        totals["requests"] += int(reqs)
        totals["tokens"] += int(tokens)
        totals["cost"] += float(cost or 0)

    workspaces = [
        WorkspaceUsageItem(
            workspace_id=wid,
            workspace_name=ws_names.get(wid, "默认工作区") if wid is not None else "默认工作区",
            projects=projects,
            requests=sum(p.requests for p in projects),
            total_tokens=sum(p.total_tokens for p in projects),
            cost=round(sum(p.cost for p in projects), 4),
        )
        for wid, projects in by_ws.items()
    ]
    workspaces.sort(key=lambda w: w.workspace_id if w.workspace_id is not None else 0)

    return UsageSummary(
        workspaces=workspaces,
        requests=totals["requests"],
        total_tokens=totals["tokens"],
        cost=round(totals["cost"], 4),
    )

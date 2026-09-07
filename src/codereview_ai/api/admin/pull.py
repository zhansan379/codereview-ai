"""主动补拉 PR/MR REST（DESIGN §9 补拉通道）：后台「补拉」按钮触发一轮主动拉取。

调 `request.app.state.poller.run_once()`，返回本轮报告（扫到项目数、打开 PR、新增审、
已审跳过、错误列表）。无 poller（worker 未启动、缺 LLM/平台）→ 503，提示先配齐再补拉。
请求级即时执行，不与定时轮询互斥（幂等键兜底同 head 去重）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from codereview_ai.api.deps import get_current_user

router = APIRouter(prefix="/pulls", dependencies=[Depends(get_current_user)])


class PollReport(BaseModel):
    projects: int
    prs: int
    new: int
    skipped: int
    errors: list[str] = []


@router.post("/poll", response_model=PollReport)
async def poll_open_pulls(request: Request) -> PollReport:
    """主动补拉一轮：扫启用项目，把打开 PR/MR 的新 head 接进审查管线（幂等去重）。"""
    poller = getattr(request.app.state, "poller", None)
    if poller is None:
        raise HTTPException(
            503, "补拉不可用：worker 未启动（需配置可用 LLM 与平台凭据）",
        )
    report: dict[str, Any] = await poller.run_once()
    return PollReport(**report)
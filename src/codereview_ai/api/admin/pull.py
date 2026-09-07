"""主动补拉 PR/MR REST（DESIGN §9 补拉通道）：后台「补拉」按钮触发一轮主动拉取。

手动补拉一轮可能会审多个 PR（每个都要 LLM 调用），耗时可达数十秒到几分钟，
不能把浏览器 HTTP 请求挂着等它完成（前端 axios 默认 30s 超时）。因此：
- `POST /pulls/poll` 把 `poller.run_once()` 放到**后台 asyncio 任务**执行，立即返回
  `{running: true}`；已有一轮在跑则 409。
- `GET /pulls/poll/status` 返回当前进度：`{running, report, error}`，前端轮询直到
  `running=false` 再展示 `report`（新审 / 已审跳过 / 错误）。
后台任务随进程存活，与页面生命周期、前端请求超时**解耦**——切页/断连都不中断补拉。
无 poller（worker 未启动、缺 LLM/平台）→ 503，提示先配齐再补拉。
"""

from __future__ import annotations

import asyncio
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


class PollStatus(BaseModel):
    running: bool
    report: PollReport | None = None
    error: str | None = None


def _poller_or_503(request: Request):
    """取 `app.state.poller`，缺失（worker 未启动）→ 503。"""
    poller = getattr(request.app.state, "poller", None)
    if poller is None:
        raise HTTPException(
            503, "补拉不可用：worker 未启动（需配置可用 LLM 与平台凭据）",
        )
    return poller


async def _background_poll(app: Any, poller: Any) -> None:
    """后台跑一轮补拉，结果写入 `app.state.poll_*`（供 /status 读取）。"""
    app.state.poll_running = True
    app.state.poll_last = None
    app.state.poll_error = None
    try:
        report: dict[str, Any] = await poller.run_once()
        app.state.poll_last = PollReport(**report)
    except Exception as exc:  # noqa: BLE001  后台任务异常只记状态，不炸请求/进程
        app.state.poll_error = f"{exc}"
        import logging

        logging.getLogger("codereview_ai.api.admin.pull").exception("补拉后台任务失败")
    finally:
        app.state.poll_running = False


@router.post("/poll", response_model=PollStatus)
async def start_poll(request: Request) -> PollStatus:
    """触发一轮主动补拉：放入后台任务立即返回；已有一轮在跑 → 409。"""
    _poller_or_503(request)
    if getattr(request.app.state, "poll_running", False):
        raise HTTPException(409, "上一轮补拉仍在后台进行，请稍候查看状态")
    task = asyncio.create_task(_background_poll(request.app, request.app.state.poller))
    request.app.state.poll_run_task = task
    return PollStatus(running=True)


@router.get("/poll/status", response_model=PollStatus)
async def poll_status(request: Request) -> PollStatus:
    """读取当前补拉状态：running + 最近一轮 report / 错误。"""
    _poller_or_503(request)
    return PollStatus(
        running=bool(getattr(request.app.state, "poll_running", False)),
        report=getattr(request.app.state, "poll_last", None),
        error=getattr(request.app.state, "poll_error", None),
    )
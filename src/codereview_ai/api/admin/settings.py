"""全局运行时设置 REST（当前：审查并发数）。

并发值存 `app_setting["worker_concurrency"]`（落 DB，重启保留）；改动经
`request.app.state.worker_pool.resize(n)` 即时热更生效，无需重启后端。

- `GET /settings/concurrency`：读当前并发（库值；缺省回落到 env
  `CR_MAX_CONCURRENT_REVIEWS`）+ `active`（worker 池是否在跑）。
- `POST /settings/concurrency`：写库并热更；`applied` = 是否已即时生效
  （worker 未启动时只落库，待运行器就绪后按库值生效——与 SchedulesView 一致的落库待生效语义）。
读/写无需 worker 也允许。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from codereview_ai.api.deps import get_current_user, require_permission
from codereview_ai.queue.concurrency import WORKER_LOOP_CAP
from codereview_ai.storage.setting_repo import SettingRepository

router = APIRouter(
    prefix="/settings",
    dependencies=[Depends(get_current_user), Depends(require_permission("settings:manage"))],
)

CONCURRENCY_KEY = "worker_concurrency"
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = WORKER_LOOP_CAP  # 与闸门 worker 循环数一致，保证升并发有闲置循环可抢


class ConcurrencyOut(BaseModel):
    concurrency: int
    active: bool
    applied: bool = True


class ConcurrencyWrite(BaseModel):
    concurrency: int = Field(ge=1, le=MAX_CONCURRENCY)


def _repo(request: Request) -> SettingRepository:
    return SettingRepository(request.app.state.engine)


def _pool(request: Request):
    """已启动的 worker 池；未启动（缺 LLM/平台）返回 None。"""
    return getattr(request.app.state, "worker_pool", None)


@router.get("/concurrency", response_model=ConcurrencyOut)
async def get_concurrency(request: Request) -> ConcurrencyOut:
    repo = _repo(request)
    default = request.app.state.settings.max_concurrent_reviews
    value = await repo.get_int(CONCURRENCY_KEY, default)
    return ConcurrencyOut(concurrency=value, active=_pool(request) is not None)


@router.post("/concurrency", response_model=ConcurrencyOut)
async def set_concurrency(
    body: ConcurrencyWrite,
    request: Request,
) -> ConcurrencyOut:
    value = min(MAX_CONCURRENCY, max(MIN_CONCURRENCY, body.concurrency))
    await _repo(request).set(CONCURRENCY_KEY, str(value))
    pool = _pool(request)
    applied = False
    if pool is not None:
        pool.resize(value)  # 热更：调大即时抢额度，调小不打断在跑审查
        applied = True
    return ConcurrencyOut(concurrency=value, active=pool is not None, applied=applied)

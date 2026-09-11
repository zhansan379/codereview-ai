"""全局运行时设置 REST（当前：审查并发数 + push/MR 自动审查默认开关 + 补拉范围）。

- 并发值存 `app_setting["worker_concurrency"]`（落 DB，重启保留）；改动经
  `request.app.state.worker_pool.resize(n)` 即时热更生效，无需重启后端。
- push 自动审查默认开关存 `app_setting["push_review_default"]`；worker 按事件热读，
  缺行回落到 env `CR_PUSH_REVIEW_ENABLED`。项目级 `push_enabled` 仍可单独覆盖。

- `GET /settings/concurrency`：读当前并发（库值；缺省回落到 env
  `CR_MAX_CONCURRENT_REVIEWS`）+ `active`（worker 池是否在跑）。
- `POST /settings/concurrency`：写库并热更；`applied` = 是否已即时生效
  （worker 未启动时只落库，待运行器就绪后按库值生效——与 SchedulesView 一致的落库待生效语义）。
- `GET /settings/push-review-default`：读全局 push 自动审查默认开关；`source` 标
  `"db"`（落库生效）或 `"env"`（无落库行，由 env `CR_PUSH_REVIEW_ENABLED` 决定）。
- `POST /settings/push-review-default`：写库并热更（worker 每事件热读即生效，不依赖重启）。
- `GET/POST /settings/poll-include-closed`：补拉范围开关（是否同时拉取已关闭/已合并 PR/MR），
  存 `app_setting["poll_include_closed"]`；缺行回落 env `CR_POLL_INCLUDE_CLOSED`，
  poller 每轮热读即生效。
读/写无需 worker 也允许。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from codereview_ai.api.deps import get_current_user, require_permission
from codereview_ai.queue.concurrency import WORKER_LOOP_CAP
from codereview_ai.storage.setting_repo import (
    MR_REVIEW_DEFAULT_KEY,
    POLL_INCLUDE_CLOSED_KEY,
    PUSH_REVIEW_DEFAULT_KEY,
    SettingRepository,
)

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


# —— push 自动审查默认开关（§7.7 全局默认；项目级仍可覆盖）——

class PushReviewDefaultOut(BaseModel):
    enabled: bool
    source: str  # "db"（落库生效）| "env"（无落库行，跟随 CR_PUSH_REVIEW_ENABLED）


class PushReviewDefaultWrite(BaseModel):
    enabled: bool


@router.get("/push-review-default", response_model=PushReviewDefaultOut)
async def get_push_review_default(request: Request) -> PushReviewDefaultOut:
    repo = _repo(request)
    env_default = request.app.state.settings.push_review_enabled
    db_value = await repo.get_bool_optional(PUSH_REVIEW_DEFAULT_KEY)
    if db_value is not None:
        return PushReviewDefaultOut(enabled=db_value, source="db")
    return PushReviewDefaultOut(enabled=env_default, source="env")


@router.post("/push-review-default", response_model=PushReviewDefaultOut)
async def set_push_review_default(
    body: PushReviewDefaultWrite, request: Request
) -> PushReviewDefaultOut:
    await _repo(request).set(PUSH_REVIEW_DEFAULT_KEY, "1" if body.enabled else "0")
    # worker 每次 push 事件热读本键即生效，无需重启后端
    return PushReviewDefaultOut(enabled=body.enabled, source="db")


# —— MR 轨自动审查默认开关（与 push 对称；项目级 mr_enabled 仍可单独覆盖）——

class MrReviewDefaultOut(BaseModel):
    enabled: bool
    source: str  # "db"（落库生效）| "env"（无落库行，跟随 CR_MR_REVIEW_ENABLED）


class MrReviewDefaultWrite(BaseModel):
    enabled: bool


@router.get("/mr-review-default", response_model=MrReviewDefaultOut)
async def get_mr_review_default(request: Request) -> MrReviewDefaultOut:
    repo = _repo(request)
    env_default = request.app.state.settings.mr_review_enabled
    db_value = await repo.get_bool_optional(MR_REVIEW_DEFAULT_KEY)
    if db_value is not None:
        return MrReviewDefaultOut(enabled=db_value, source="db")
    return MrReviewDefaultOut(enabled=env_default, source="env")


@router.post("/mr-review-default", response_model=MrReviewDefaultOut)
async def set_mr_review_default(
    body: MrReviewDefaultWrite, request: Request
) -> MrReviewDefaultOut:
    await _repo(request).set(MR_REVIEW_DEFAULT_KEY, "1" if body.enabled else "0")
    # worker 每次 MR 事件热读本键即生效，无需重启后端
    return MrReviewDefaultOut(enabled=body.enabled, source="db")


# —— 补拉范围开关（§9：是否同时拉取已关闭/已合并的 PR/MR）——

class PollIncludeClosedOut(BaseModel):
    enabled: bool
    source: str  # "db"（落库生效）| "env"（无落库行，跟随 CR_POLL_INCLUDE_CLOSED）


class PollIncludeClosedWrite(BaseModel):
    enabled: bool


@router.get("/poll-include-closed", response_model=PollIncludeClosedOut)
async def get_poll_include_closed(request: Request) -> PollIncludeClosedOut:
    repo = _repo(request)
    env_default = request.app.state.settings.poll_include_closed
    db_value = await repo.get_bool_optional(POLL_INCLUDE_CLOSED_KEY)
    if db_value is not None:
        return PollIncludeClosedOut(enabled=db_value, source="db")
    return PollIncludeClosedOut(enabled=env_default, source="env")


@router.post("/poll-include-closed", response_model=PollIncludeClosedOut)
async def set_poll_include_closed(
    body: PollIncludeClosedWrite, request: Request
) -> PollIncludeClosedOut:
    await _repo(request).set(POLL_INCLUDE_CLOSED_KEY, "1" if body.enabled else "0")
    # poller 每轮热读本键即生效，无需重启后端
    return PollIncludeClosedOut(enabled=body.enabled, source="db")

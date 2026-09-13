"""一次性回填：给存量 review_task 补 `pr_created_at`（PR/MR 平台真实创建时间）。

背景：提交分析（/admin/workrate）此前对 mr 轨只能用 `queued_at` 入队时间——主动补拉
落库的行 `payload` 为空、且旧版不存平台创建时间，导致「24 小时分布」被补拉时刻污染。
现在新增 `pr_created_at` 列（新数据由 ensure_task/poller 自动写入），本脚本把存量行补齐：

两层来源，逐行先用尽免费数据再走网络：
1. payload 提取（零网络）：webhook 行的原始 JSON 里本就有
   `pull_request.created_at`（GitHub/Gitee）/ `object_attributes.created_at`（GitLab）；
2. 平台 API（按仓库一次 list_pulls，含已关闭/已合并）：按 (provider, repo_id) 分组
   批量映射 pr_number → created_at。凭据从 DB（设置页）或 env 解析，与应用同款装配；
   某平台无凭据/列 PR 失败 → 该组跳过并 WARNING，不影响其他组。

用法：
    .venv/Scripts/python.exe scripts/backfill_pr_created_at.py
    # 默认连 DATABASE_URL 环境变量或 sqlite:///./data/app.db；可用 --url 覆盖
    # 只做离线提取（不调平台 API）：--offline

幂等：只处理 `event_type='mr'` 且 `pr_created_at IS NULL` 的行；写出后不再入选。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codereview_ai.domain.models import as_naive_utc, parse_forge_datetime
from codereview_ai.storage.db import create_engine, init_db, session_factory
from codereview_ai.storage.models import Project, ReviewTask

DEFAULT_URL = os.environ.get(
    "DATABASE_URL",
    os.environ.get("CR_DATABASE_URL", "sqlite:///./data/app.db"),
)


def _created_at_from_payload(payload_raw: str) -> datetime | None:
    """从原始 webhook JSON 提取 PR/MR 创建时间；都无则 None。"""
    try:
        data = json.loads(payload_raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("pull_request", "object_attributes"):
        node = data.get(key)
        if isinstance(node, dict):
            return parse_forge_datetime(node.get("created_at"))
    return None


async def _backfill_offline(
    session_fac: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    """第一层：payload 提取。返回 (回填数, 仍缺数)。"""
    changed = still_missing = 0
    async with session_fac() as s:
        rows = (await s.execute(
            select(ReviewTask).where(
                ReviewTask.event_type == "mr", ReviewTask.pr_created_at.is_(None)
            )
        )).scalars().all()
        for row in rows:
            dt = _created_at_from_payload(row.payload or "")
            if dt is None:
                still_missing += 1
                continue
            row.pr_created_at = as_naive_utc(dt)  # DB 口径 naive UTC
            changed += 1
        await s.commit()
    return changed, still_missing


async def _backfill_via_api(url: str) -> tuple[int, list[str]]:
    """第二层：按 (provider, repo_id) 调平台 API 补；返回 (回填数, 错误列表)。"""
    from codereview_ai.config.repository import ConfigRepository
    from codereview_ai.config.settings import Settings
    from codereview_ai.forges.registry import ForgeRegistry

    engine = create_engine(url)
    settings = Settings()  # 读 .env：CR_ENCRYPTION_KEY 等
    provider_repo = ConfigRepository(engine, encryption_key=settings.encryption_key)
    registry = ForgeRegistry(provider_repo, httpx.AsyncClient(timeout=30.0))
    await registry.refresh_all()

    changed = 0
    errors: list[str] = []
    session = session_factory(engine)
    async with session() as s:
        # 待补行按 (provider, repo_id) 分组；仓库展示名一并取出便于报错可读
        groups: dict[tuple[str, str], list[ReviewTask]] = {}
        rows = (await s.execute(
            select(ReviewTask).where(
                ReviewTask.event_type == "mr", ReviewTask.pr_created_at.is_(None)
            )
        )).scalars().all()
        for row in rows:
            groups.setdefault((row.provider, row.repo_id), []).append(row)
        proj_names = {
            (p.provider, p.repo_id): (p.repo_full_name or p.repo_id)
            for p in (await s.execute(select(Project))).scalars().all()
        }
        for (provider, repo_id), tasks in groups.items():
            adapter = registry.get(provider)
            if adapter is None:
                errors.append(
                    f"{provider}:{repo_id} 无可用适配器（该平台未配置凭据），"
                    f"跳过 {len(tasks)} 行"
                )
                continue
            try:
                # 含已关闭/已合并：历史行多半已合码，只列 open 会漏
                prs = await adapter.list_pulls(repo_id, include_closed=True)
            except Exception as exc:  # noqa: BLE001 — 单仓库失败隔离
                errors.append(
                    f"{provider}:{proj_names.get((provider, repo_id), repo_id)} "
                    f"列 PR 失败：{exc}，跳过 {len(tasks)} 行"
                )
                continue
            created_by_number = {
                pr.pr_number: pr.created_at for pr in prs if pr.created_at is not None
            }
            hit = 0
            for row in tasks:
                dt = created_by_number.get(row.pr_number or 0)
                if dt is None:
                    continue
                row.pr_created_at = as_naive_utc(dt)  # DB 口径 naive UTC
                hit += 1
            changed += hit
            name = proj_names.get((provider, repo_id), repo_id)
            print(f"  {provider}:{name} → {hit}/{len(tasks)} 行")
        await s.commit()
    return changed, errors


async def main(url: str, *, offline: bool = False) -> None:
    engine = create_engine(url)
    await init_db(engine)  # 确保表结构与 pr_created_at 列存在
    session = session_factory(engine)

    changed, still_missing = await _backfill_offline(session)
    print(f"① payload 提取：回填 {changed} 条，仍缺 {still_missing} 条")

    if not offline and still_missing:
        print("② 平台 API 补齐（按仓库一次列 PR，含已关闭/已合并）……")
        api_changed, errors = await _backfill_via_api(url)
        changed += api_changed
        for line in errors:
            print(f"  WARNING {line}")

    async with session() as s:
        remaining = len((await s.execute(
            select(ReviewTask.id).where(
                ReviewTask.event_type == "mr", ReviewTask.pr_created_at.is_(None)
            )
        )).fetchall())
    print(f"完成：共回填 {changed} 条 review_task.pr_created_at；剩余无来源 {remaining} 条"
          f"（这些行提交分析继续按入队时间兜底）")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="数据库 URL（默认 DATABASE_URL 或 sqlite）")
    parser.add_argument("--offline", action="store_true", help="只做 payload 提取，不调平台 API")
    args = parser.parse_args()
    asyncio.run(main(args.url, offline=args.offline))

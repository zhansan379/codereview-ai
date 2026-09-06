"""审查记录持久化仓储（DESIGN §7.3 / §5)：以 DB 为增量落点，替代 M3 的内存档。

增量决策所需的「上次成功审查的 head_sha + 内容指纹」原本存进程内 `IncrementStore`（M4 前）。
本仓储从 `review_task`（state='completed'）取 `last_reviewed_sha`，从 `review_finding`
取 `fingerprint` 集，供 `decide_from_ref` 做增量决定与去重——落点天然随 DB 持久化，
重启/多 worker 共享，无需显式 `record`（findings 已由结果写入器落库）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.review.increments import IncrementReference
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask


class ReviewRepository:
    """读取/写入 `review_task` 的仓储；当前只实现增量决策所需的最小读路径。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def last_ok_review(
        self, provider: str, repo_id: str, pr_number: int
    ) -> IncrementReference | None:
        """返回该 PR 最近一次完成审查的落点；从未成功审过则 None。

        取 `state='completed'` 的最新一条 mr 轨记录作为可复用落点；
        `head_sha` 作 `last_reviewed_sha`，`review_finding.fingerprint` 集作内容指纹。
        """
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask)
                .where(
                    ReviewTask.provider == provider,
                    ReviewTask.repo_id == repo_id,
                    ReviewTask.pr_number == pr_number,
                    ReviewTask.event_type == "mr",
                    ReviewTask.state == "completed",
                )
                .order_by(ReviewTask.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            if row is None or not row.head_sha:
                return None
            fingerprint_rows = (await s.execute(
                select(ReviewFinding.fingerprint).where(ReviewFinding.task_id == row.id)
            )).scalars().all()
        return IncrementReference(row.head_sha, frozenset(fingerprint_rows))

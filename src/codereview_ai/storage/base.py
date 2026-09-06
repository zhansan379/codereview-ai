"""持久化抽象接口（DESIGN §8.1）。

业务层/worker 只依赖这里的抽象，不 import SQLAlchemy —— 这样测试能用内存版仓储替换，
存储实现也能在两档（SQLite/PostgreSQL）间切换而不透传到上层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from codereview_ai.domain.models import PullRequest


class UnitOfWork(ABC):
    """工作单元：一组数据库操作要么全提交要么全回滚。"""

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def __aenter__(self) -> UnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc and exc[0] is not None:
            await self.rollback()
        else:
            await self.commit()
        await self.close()


class ReviewRepository(ABC):
    """审查任务/结论的仓储。幂等抢占靠 DB 部分唯一索引（DESIGN §5）。"""

    @abstractmethod
    async def create_review_task(
        self,
        uow: UnitOfWork,
        *,
        pr: PullRequest | None,
        event_type: str,
        branch: str,
        head_sha: str,
        base_sha: str | None,
    ) -> int:
        """入库 `queued` 任务并返回 id；冲突（同 head_sha）抛异常或返回已存在标记。"""  # noqa: E501

    @abstractmethod
    async def exists(self, uow: UnitOfWork, *, head_sha: str, branch: str, event_type: str) -> bool:
        """按幂等键判断是否已存在（供重复 webhook 直接 200 跳过）。"""

    @abstractmethod
    async def last_ok_review(self, uow: UnitOfWork, *, repo_id: str, pr_number: int) -> object | None:  # noqa: E501
        """取上次 `succeeded` 且 head 落在本次 base 链上的审查记录（增量审查用）。"""

    @abstractmethod
    async def list_tasks(
        self,
        uow: UnitOfWork,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[object]:
        """审查任务列表（后台/任务监控用）。"""

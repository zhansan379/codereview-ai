"""审查记录持久化仓储（DESIGN §7.3 / §5)：以 DB 为增量落点 + push 轨审计落库。

增量决策所需的「上次成功审查的 head_sha + 内容指纹」读 `review_task`(completed) 与
`review_finding.fingerprint`，供 `decide_from_ref` 做增量决定与去重（M4.5）。
M5.1/M5.2 起 worker 通过 `ensure_task`/`mark_state`/`insert_findings` **真正把
review_task/review_finding 落库**：两轨（mr/push）幂等抢占靠部分唯一索引，push 轨
（§7.7）事件始终落一条审计行再决定是否走 LLM。findings 落库后由 `last_ok_review`
读取作增量指纹，无需显式 `record`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.domain.models import Finding
from codereview_ai.review.increments import IncrementReference, finding_fingerprint
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ReviewFinding, ReviewTask

#: 短标题缺失时（静态/旧 LLM）由完整分析 content 兜底截断。
_MAX_TITLE = 40


def _finding_title(f: Finding) -> str:
    t = (f.title or "").strip()
    if t:
        return t
    c = (f.content or "").strip()
    return c if len(c) <= _MAX_TITLE else c[:_MAX_TITLE - 1] + "…"


class ReviewRepository:
    """读写 `review_task`/`review_finding` 的仓储：增量决策读路径 + 结果持久化写路径。"""

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

    async def push_existing_audit(
        self, *, provider: str, repo_id: str, branch: str, head_sha: str
    ) -> ReviewTask | None:
        """push 轨幂等预检：同 `(provider, repo_id, branch, head_sha)` 是否已有审计行。

        返回行对象（而非 bool），供调用方读 `force_rerun` 判断是否为手动重试：
        - 普通重复 webhook（force_rerun=false）→ 该分支该 after 已处理过，跳过（DESIGN §7.7）；
        - 手动重试（force_rerun=true）→ 调用方据此绕过跳过、强制执行。
        """
        session = session_factory(self._engine)
        async with session() as s:
            existing = (await s.execute(
                select(ReviewTask).where(
                    ReviewTask.provider == provider,
                    ReviewTask.repo_id == repo_id,
                    ReviewTask.event_type == "push",
                    ReviewTask.branch == branch,
                    ReviewTask.head_sha == head_sha,
                ).limit(1)
            )).scalar_one_or_none()
        return existing

    async def clear_force_rerun(self, task_id: int) -> None:
        """手动重试意图已消费，清掉 `force_rerun`（worker 强制执行后调用）。"""
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.force_rerun = False
            await s.commit()

    async def ensure_task(
        self,
        *,
        provider: str,
        repo_id: str,
        pr_number: int | None,
        event_type: str,
        branch: str,
        head_sha: str,
        base_sha: str = "",
        pr_title: str = "",
        payload: str = "",
    ) -> int | None:
        """按幂等键幂等落一条 `queued` 审计行并返回 id；已存在/并发冲突返回 None。

        幂等键（DESIGN §5）：mr 轨 `(provider, repo_id, pr_number, head_sha)`、
        push 轨 `(provider, repo_id, event_type, branch, head_sha)`——由对应部分唯一索引
        抢占，冲突即视为已处理（§7.7 幂等），调用方据此跳过。
        """
        session = session_factory(self._engine)
        async with session() as s:
            stmt = select(ReviewTask.id).where(
                ReviewTask.provider == provider,
                ReviewTask.repo_id == repo_id,
                ReviewTask.head_sha == head_sha,
                ReviewTask.event_type == event_type,
            )
            if event_type == "push":
                stmt = stmt.where(ReviewTask.branch == branch)
            else:
                stmt = stmt.where(ReviewTask.pr_number == pr_number)
            existing = (await s.execute(stmt.limit(1))).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            task = ReviewTask(
                provider=provider, repo_id=repo_id, pr_number=pr_number,
                event_type=event_type, branch=branch, head_sha=head_sha,
                base_sha=base_sha, pr_title=pr_title, state="queued", payload=payload,
            )
            s.add(task)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()  # 并发下另一 worker 已插 → 幂等跳过
                return None
            return int(task.id)

    async def mark_state(
        self,
        task_id: int,
        *,
        state: str,
        error: str = "",
        summary_md: str = "",
        score_total: int = 0,
        skip_reason: str = "",
    ) -> None:
        """更新一条审查任务的状态（queued→skipped/completed/failed）。

        `skip_reason` 给 skipped 分型（push_disabled/branch_mismatch/branch_deleted，
        DESIGN §7.7），供前端/重试判定该跳过是否可补审。
        """
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.state = state
            row.error = error
            if skip_reason:
                row.skip_reason = skip_reason
            if summary_md:
                row.summary_md = summary_md
            row.score_total = score_total
            if state in {"completed", "failed"}:
                from codereview_ai.storage.models import _utcnow

                row.finished_at = _utcnow()
            await s.commit()

    async def insert_findings(
        self, task_id: int, findings: list[Finding], *, skip_fingerprints: frozenset[str] = frozenset()
    ) -> None:
        """把一轮 findings 落成 `review_finding` 行（含 source 区分 llm/static/agent）。

        `skip_fingerprints`：复现对账后已由 `reconcile_findings` 回的指纹，跳过不重复插入
        （避免复现 finding 产生第二条 active 行）。
        """
        session = session_factory(self._engine)
        async with session() as s:
            for f in findings:
                if finding_fingerprint(f) in skip_fingerprints:
                    continue
                s.add(ReviewFinding(
                    task_id=task_id,
                    fingerprint=finding_fingerprint(f),
                    severity=str(f.severity),
                    category=str(f.category),
                    file=f.file or "",
                    old_line=f.old_line if f.side == "LEFT" else None,
                    new_line=f.line if f.side == "RIGHT" else None,
                    existing_code=f.existing_code,
                    title=_finding_title(f),
                    detail=f.content,
                    suggestion=f.suggestion_code or "",
                    source=f.source,
                    status="active",
                ))
            await s.commit()

    async def reconcile_findings(
        self,
        *,
        provider: str,
        repo_id: str,
        pr_number: int,
        current_findings: list[Finding],
        covered_files: set[str],
        exclude_task_id: int,
    ) -> frozenset[str]:
        """非增量全量轮次的 finding 生命周期对账（DESIGN §7.3），推进 `status` 状态机。

        仅处理 `file` 落在本轮 `covered_files` 的行——本轮没覆盖到的缺席**不算**已解决
        （§7.3 保守门，防部分审查误判）。逐行：
        - `active` 且本轮缺席 → `resolved`（覆盖到已修复）；
        - `active` 且本轮仍在 → 刷新 `last_seen`；
        - `resolved` 且本轮复现 → 回 `active`，`reopened_count += 1`；
        - `waived` 永不自动改（人工忽略保持忽略）。

        返回「复现并回 active 的指纹集」，供 `insert_findings` 作 `skip_fingerprints` 去重。
        """
        from codereview_ai.storage.models import _utcnow

        cur_fps = frozenset(finding_fingerprint(f) for f in current_findings)
        reopen: set[str] = set()
        session = session_factory(self._engine)
        async with session() as s:
            prior = (await s.execute(
                select(ReviewFinding).where(
                    ReviewFinding.task_id.in_(
                        select(ReviewTask.id).where(
                            ReviewTask.provider == provider,
                            ReviewTask.repo_id == repo_id,
                            ReviewTask.pr_number == pr_number,
                            ReviewTask.event_type == "mr",
                            ReviewTask.state == "completed",
                            ReviewTask.id != exclude_task_id,
                        )
                    )
                )
            )).scalars().all()
            now = _utcnow()
            for row in prior:
                if row.file not in covered_files:
                    continue  # 本轮没覆盖到 → 不判已解决（保守门）
                if row.fingerprint in cur_fps:
                    if row.status == "resolved":
                        row.status = "active"
                        row.reopened_count += 1
                        row.last_seen = now
                        reopen.add(row.fingerprint)
                    elif row.status == "active":
                        row.last_seen = now
                    # waived：永不自动改
                elif row.status == "active":
                    row.status = "resolved"
                    row.last_seen = now
            await s.commit()
        return frozenset(reopen)

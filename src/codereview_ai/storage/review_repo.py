"""审查记录持久化仓储（DESIGN §7.3 / §5)：以 DB 为增量落点 + push 轨审计落库。

增量决策所需的「上次成功审查的 head_sha + 内容指纹」读 `review_task`(completed) 与
`review_finding.fingerprint`，供 `decide_from_ref` 做增量决定与去重（M4.5）。
M5.1/M5.2 起 worker 通过 `ensure_task`/`mark_state`/`insert_findings` **真正把
review_task/review_finding 落库**：两轨（mr/push）幂等抢占靠部分唯一索引，push 轨
（§7.7）事件始终落一条审计行再决定是否走 LLM。findings 落库后由 `last_ok_review`
读取作增量指纹，无需显式 `record`。
"""

from __future__ import annotations

import json

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


def _cast(value: str | None, enum_type: type, fallback):
    """把入库的中立字符串安全转回 StrEnum；异常值回落 fallback，绝不因脏数据崩重发。"""
    if not value:
        return fallback
    try:
        return enum_type(value)
    except ValueError:
        return fallback


class ReviewRepository:
    """读写 `review_task`/`review_finding` 的仓储：增量决策读路径 + 结果持久化写路径。"""

    #: 补拉「已处理可跳过」的 mr 终态/在途态（DESIGN §9 补拉通道）。failed 不在内：
    #: 该 head 从未成功审过，补拉到 failed 行仍重新入队作重试（显式「重试」按钮亦可用）。
    POLL_SKIP_STATES = ("queued", "running", "completed", "skipped")

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def poll_skip(
        self, provider: str, repo_id: str, pr_number: int, head_sha: str
    ) -> bool:
        """补拉跳过判定：该 PR 同 head 是否已有一条「视为已处理」的 mr 任务行。

        已处理态（`queued`/`running`/`completed`/`skipped`）→ True（不再入队、计数为
        skipped）；`failed` 或从无行 → False（failed 留接口显式「重试」，从无行 = 全新
        head 入队待审）。与 `last_ok_review` 的语义区分：后者专供增量决策读「上次**成功**
        审查」的 head_sha，只认 completed；此处是补拉的「是否已对该 head 下过判断」，把
        `skipped`（如 mr_disabled 门控跳过）也算已处理，避免每次补拉把存量跳过行当成
        「新入队」重复投队。
        """
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(
                    ReviewTask.provider == provider,
                    ReviewTask.repo_id == repo_id,
                    ReviewTask.pr_number == pr_number,
                    ReviewTask.event_type == "mr",
                    ReviewTask.head_sha == head_sha,
                ).limit(1)
            )).scalar_one_or_none()
        if row is None:
            return False
        return row.state in self.POLL_SKIP_STATES

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

    async def pending_for_replay(self) -> list[tuple[int, str, str]]:
        """启动回放候选：遗留 `state IN ('queued','running')` 且带 payload 的任务。

        返回 `[(task_id, provider, payload)]`。`running` 行（上次崩溃残留）先复位回
        `queued`，避免悬死。重启后据此把卡死的任务重新入队续跑——幂等安全：同 head
        已 completed 的重放会被增量决策/`ensure_task` 短路，不重复审查也不重复写 finding。
        """
        session = session_factory(self._engine)
        async with session() as s:
            rows = (await s.execute(
                select(ReviewTask)
                .where(ReviewTask.state.in_(("queued", "running")), ReviewTask.payload != "")
                .order_by(ReviewTask.id)
            )).scalars().all()
            out: list[tuple[int, str, str]] = []
            for r in rows:
                if r.state == "running":
                    r.state = "queued"  # 崩溃残留复位，避免重启后仍悬死
                out.append((int(r.id), r.provider, r.payload))
            await s.commit()
        return out

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

    async def force_rerun_flag(self, task_id: int) -> bool:
        """读行的 `force_rerun`（手动重试意图标记）；行不存在 → False。

        供 mr 轨 `_do_review_pull_request` 在门控前判断是否为手动补审：True 则绕过
        门控强审（与 push 轨读 `push_existing_audit.force_rerun` 同义）。
        """
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
        return bool(row and row.force_rerun)

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
        web_url: str = "",
        push_commits: str = "",
        payload: str = "",
        trace_id: str = "",
        diff_snapshot: str = "",
    ) -> int | None:
        """按幂等键幂等落一条 `queued` 审计行并返回 id；已被抢占/在审返回 None。

        幂等键（DESIGN §5）：mr 轨 `(provider, repo_id, pr_number, head_sha)`、
        push 轨 `(provider, repo_id, event_type, branch, head_sha)`——由对应部分唯一索引
        抢占，冲突即视为已处理（§7.7 幂等），调用方据此跳过。

        同 head 已有任务行时：
        - `running`（另一生产者**正在审**）→ 返回 None，调用方应幂等跳过，防止并发重复审查
          重复刷评论；
        - `queued`（等待 worker 消费/本生产者的入队行）→ 返回该 id，调用方照常处理并翻
          running（队列消费者即该行的拥有者）；终态（`failed`/`completed`/`skipped`）→ 返回
          该 id，供调用方重试/复用（已失败的 head 不应被永久跳过）。
        """
        session = session_factory(self._engine)
        async with session() as s:
            stmt = select(ReviewTask).where(
                ReviewTask.provider == provider,
                ReviewTask.repo_id == repo_id,
                ReviewTask.head_sha == head_sha,
                ReviewTask.event_type == event_type,
            )
            if event_type == "push":
                stmt = stmt.where(ReviewTask.branch == branch)
            else:
                stmt = stmt.where(ReviewTask.pr_number == pr_number)
            existing = (await s.execute(stmt.limit(1))).scalars().first()
            if existing is not None:
                # 正在被别的生产者审 → 跳过；排队中/终态 → 可处理（队列消费者即拥有者）
                if existing.state == "running":
                    return None
                return int(existing.id)
            task = ReviewTask(
                provider=provider, repo_id=repo_id, pr_number=pr_number,
                event_type=event_type, branch=branch, head_sha=head_sha,
                base_sha=base_sha, pr_title=pr_title, web_url=web_url,
                push_commits=push_commits, state="queued", payload=payload,
                trace_id=trace_id, diff_snapshot=diff_snapshot,
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
        """更新一条审查任务的状态（queued→running→skipped/completed/failed）。

        `running` 写入 `started_at`（worker 开审即标），completed/failed 写 `finished_at`，
        供前端区分「正在跑」（running）与「等待/崩溃孤儿」（queued）。
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
            elif state == "running":
                # worker 开审即标 running（DESIGN §9.2）：落 started_at，前端可区分
                # 「正在跑」（running）与「排队/孤儿」（queued）。
                from codereview_ai.storage.models import _utcnow

                row.started_at = _utcnow()
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

    async def set_coverage(self, task_id: int, covered: dict[str, str]) -> None:
        """把本轮覆盖集 `{new_path: sha1(new)}` 写进 `diff_snapshot`（复用 + compare 依据）。"""
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.diff_snapshot = json.dumps(covered, ensure_ascii=False, sort_keys=True)
            await s.commit()

    async def findings_for_task(self, task_id: int) -> list[Finding]:
        """把该任务已落库的 `review_finding` 行反映射回 `Finding`（重发评论用）。

        与 `insert_findings` 的字段映射成逆运算：LEFT 存 `old_line`、RIGHT 存 `new_line`。
        """
        from codereview_ai.domain.models import Category, Severity

        session = session_factory(self._engine)
        async with session() as s:
            rows = (await s.execute(
                select(ReviewFinding).where(ReviewFinding.task_id == task_id)
                .order_by(ReviewFinding.id)
            )).scalars().all()
        out: list[Finding] = []
        for r in rows:
            side = "RIGHT" if r.new_line is not None else "LEFT"
            out.append(Finding(
                content=r.detail or "",
                category=_cast(r.category, Category, Category.OTHER),
                severity=_cast(r.severity, Severity, Severity.MEDIUM),
                existing_code=r.existing_code or "",
                file=r.file or "",
                title=r.title or "",
                suggestion_code=r.suggestion or None,
                line=r.new_line if side == "RIGHT" else None,
                old_line=r.old_line if side == "LEFT" else None,
                side=side,
                source=r.source or "llm",
            ))
        return out

    async def mark_writeback(self, task_id: int, failed: bool) -> None:
        """翻转任务的 `writeback_failed`（回写失败置 True 供前端展示「重新发送」；重发成功归位）。"""
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.writeback_failed = failed
            await s.commit()

    async def set_exec_metrics(
        self,
        task_id: int,
        *,
        exec_mode: str = "diff",
        diff_lines: int = 0,
        chat_rounds: int = 0,
        tool_calls: int = 0,
    ) -> None:
        """写执行态快照四列（仪表盘 agent/diff 区分 + 复杂度×成本散点依据）。

        `exec_mode` 记录**实际跑通**的路径（agentic 降级时由调用方落 'diff'）。
        """
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(ReviewTask.id == task_id)
            )).scalar_one_or_none()
            if row is None:
                return
            row.exec_mode = exec_mode
            row.diff_lines = diff_lines
            row.chat_rounds = chat_rounds
            row.tool_calls = tool_calls
            await s.commit()

    async def last_covered(
        self, provider: str, repo_id: str, pr_number: int
    ) -> dict[str, str] | None:
        """读该 PR 最近一次 completed mr 任务的覆盖集；未审过/无覆盖 → None。"""
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(ReviewTask).where(
                    ReviewTask.provider == provider,
                    ReviewTask.repo_id == repo_id,
                    ReviewTask.pr_number == pr_number,
                    ReviewTask.event_type == "mr",
                    ReviewTask.state == "completed",
                )
                .order_by(ReviewTask.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            if row is None or not row.diff_snapshot:
                return None
        try:
            data = json.loads(row.diff_snapshot)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    async def findings_for_task(self, task_id: int) -> list[ReviewFinding]:
        """任务全部 finding 行（供 compare 取 after 集）。"""
        session = session_factory(self._engine)
        async with session() as s:
            rows = (await s.execute(
                select(ReviewFinding).where(ReviewFinding.task_id == task_id)
                .order_by(ReviewFinding.id)
            )).scalars().all()
            return list(rows)

    async def previous_completed_task(
        self, provider: str, repo_id: str, pr_number: int, exclude_task_id: int
    ) -> ReviewTask | None:
        """该 PR 上一次（排除本任务）completed mr 任务，供 compare 取 before 集。"""
        session = session_factory(self._engine)
        async with session() as s:
            return (await s.execute(
                select(ReviewTask).where(
                    ReviewTask.provider == provider,
                    ReviewTask.repo_id == repo_id,
                    ReviewTask.pr_number == pr_number,
                    ReviewTask.event_type == "mr",
                    ReviewTask.state == "completed",
                    ReviewTask.id != exclude_task_id,
                )
                .order_by(ReviewTask.id.desc())
                .limit(1)
            )).scalar_one_or_none()

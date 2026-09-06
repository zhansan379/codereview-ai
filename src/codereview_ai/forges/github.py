"""GitHub 适配器：payload → PullRequest；files → FileDiff；批量 review（DESIGN §9/§13.2）。

字段路径对照 `reference/platform_payload_map.md` §3/§4。
- webhook 的 `pull_request.head.sha` 是幂等键 head_sha；`base_sha` 直接用 `pull_request.base.sha`。
- files API 一次返回全部改动文件，每项自带 unified `patch`、`additions/deletions`、`status`。
- 新增文件（status=added）的全文可从 patch 直接还原（全是 `+` 行），供锚定定位的全文兜底。
- GitHub 的 changes 同样可能延迟返回空数组 → `asyncio.sleep` + 指数退避重试（与 GitLab 一致）。
- 回写：总结走 `issues/{n}/comments`，行级走单次 `pulls/{n}/reviews` 批量（event=COMMENT）。
- 发批量的 HTTP 客户端在构造时注入（测试用 httpx.MockTransport，离线可测）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx

from codereview_ai.domain.models import ChangeType, FileDiff, PullRequest
from codereview_ai.forges.base import ForgeAdapter
from codereview_ai.forges.signatures import GITHUB

#: 空 files 数组时指数退避的初始/最大延时（秒）。
_RETRY_DELAY_0 = 0.5
_RETRY_ATTEMPTS = 3

#: GitHub files API 的 status → ChangeType。
_STATUS_TO_CHANGE = {
    "added": ChangeType.NEW_FILE,
    "removed": ChangeType.DELETED_FILE,
    "renamed": ChangeType.RENAMED_FILE,
    "modified": ChangeType.MODIFIED,
    "changed": ChangeType.MODIFIED,
    "copied": ChangeType.MODIFIED,
}


def parse_pull_request_payload(data: dict[str, Any]) -> PullRequest | None:
    """从 GitHub `pull_request` webhook payload 解析中立 PullRequest。

    非 pull_request 事件返回 None。
    """
    event = data.get("pull_request") or {}
    if not isinstance(event, dict) or not event.get("number"):
        return None
    repo = data.get("repository") or {}
    if not isinstance(repo, dict):
        repo = {}
    full_name = str(repo.get("full_name") or "")
    head = event.get("head") or {}
    base = event.get("base") or {}
    if not isinstance(head, dict):
        head = {}
    if not isinstance(base, dict):
        base = {}

    return PullRequest(
        provider=GITHUB,
        repo_id=full_name,  # GitHub 项目唯一 id = "owner/name"（DESIGN §5）
        repo_full_name=full_name,
        web_url=str(event.get("html_url") or repo.get("html_url") or ""),
        pr_number=int(event.get("number") or 0),
        title=str(event.get("title") or ""),
        source_branch=str(head.get("ref") or ""),
        target_branch=str(base.get("ref") or ""),
        head_sha=str(event.get("head_sha") or head.get("sha") or ""),
        base_sha=str(base.get("sha") or ""),
        author=str(((data.get("sender") or {}) or {}).get("login") or ""),
        is_draft=bool(event.get("draft")),
    )


def _to_file_diff(item: dict[str, Any]) -> FileDiff:
    new_path = str(item.get("filename") or "")
    change = _STATUS_TO_CHANGE.get(str(item.get("status") or ""), ChangeType.MODIFIED)
    old_path = str(item.get("previous_filename") or new_path)
    if change is ChangeType.DELETED_FILE:
        # 删除文件没有新路径；行级评论挂 LEFT + old_line，路径用旧文件名
        new_path = ""
    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        diff=str(item.get("patch") or ""),
        additions=int(item.get("additions") or 0),
        deletions=int(item.get("deletions") or 0),
        change_type=change,
        new_file_content=_new_file_content_from_patch(item.get("patch") or "", change),
    )


def _new_file_content_from_patch(patch: str, change: ChangeType) -> str:
    """新增文件：patch 全是 `+` 行，剥前缀还原全文（供锚定全文兜底）。"""
    if change is not ChangeType.NEW_FILE:
        return ""
    lines: list[str] = []
    for raw in patch.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
    if not lines:
        return ""
    return "\n".join(lines)


def _owner_repo(repo_id: str) -> tuple[str, str]:
    """repo_id="owner/name" → (owner, name)。"""
    parts = str(repo_id).split("/")
    owner = parts[0] if parts else ""
    repo = parts[1] if len(parts) > 1 else ""
    return owner, repo


class GitHubForge(ForgeAdapter):
    """GitHub 平台适配器：围绕注入的 httpx client 实现 ForgeAdapter。"""

    name = GITHUB

    def __init__(self, api_base: str, token: str, http: httpx.AsyncClient) -> None:
        self._base = str(api_base).rstrip("/")
        self._token = token
        self._http = http

    # ── payload → 中立模型 ──────────────────────────────────────────────
    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        return parse_pull_request_payload(data)

    # ── REST 拉取 ──────────────────────────────────────────────────────
    async def fetch_pull_request(self, pr: PullRequest) -> PullRequest:
        """GET /pulls/{n} 补齐权威 head_sha/base_sha/title（批量 review 的 commit_id 必填）。"""
        owner, repo = _owner_repo(pr.repo_id)
        resp = await self._http.get(
            f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            return pr
        head = body.get("head") or {}
        base = body.get("base") or {}
        return replace(
            pr,
            head_sha=str((head if isinstance(head, dict) else {}).get("sha") or pr.head_sha),
            base_sha=str((base if isinstance(base, dict) else {}).get("sha") or pr.base_sha),
            title=str(body.get("title") or pr.title),
        )

    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        """GET /pulls/{n}/files；空数组时指数退避重试。"""
        owner, repo = _owner_repo(pr.repo_id)
        path = f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/files"
        delay = _RETRY_DELAY_0
        for attempt in range(_RETRY_ATTEMPTS):
            resp = await self._http.get(path, headers=self._auth_headers())
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, list) and body:
                return [_to_file_diff(i) for i in body if isinstance(i, dict)]
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(delay)  # 协程等待，不阻塞事件循环
                delay *= 2
        return []

    # ── 回写评论 ───────────────────────────────────────────────────────
    async def post_summary(self, pr: PullRequest, body: str) -> None:
        """整体总结：POST /issues/{n}/comments，字段名 body。"""
        owner, repo = _owner_repo(pr.repo_id)
        resp = await self._http.post(
            f"{self._base}/repos/{owner}/{repo}/issues/{pr.pr_number}/comments",
            headers=self._auth_headers(),
            json={"body": body},
        )
        resp.raise_for_status()

    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        """逐条行级评论：单次 `pulls/{n}/reviews` 批量（event=COMMENT），只发一封通知。

        comments 元素用 path/line/side（锚点，非会漂移的 legacy position，DESIGN §13.2）；
        删行（side=LEFT）用 old_line，新增/修改用 line。行号已在 result_writer 层按
        可评论行集合过滤，此处不再校验。缺失 head_sha 或行号时不投。
        """
        owner, repo = _owner_repo(pr.repo_id)
        if not pr.head_sha:
            return
        batch = []
        for c in comments:
            path = c.get("path") or ""
            side = c.get("side")
            line = c.get("old_line") if side == "LEFT" else c.get("line")
            if not path or not line:
                continue
            batch.append({
                "path": path,
                "line": int(line),
                "side": side or "RIGHT",
                "body": str(c.get("body") or ""),
            })
        if not batch:
            return
        resp = await self._http.post(
            f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/reviews",
            headers=self._auth_headers(),
            json={"commit_id": pr.head_sha, "event": "COMMENT", "comments": batch},
        )
        resp.raise_for_status()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github.v3+json"}  # noqa: E501

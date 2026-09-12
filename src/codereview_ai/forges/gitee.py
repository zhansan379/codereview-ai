"""Gitee（gitee.com，API v5）适配器：payload → PullRequest；files → FileDiff；评论回写。

与 GitHub 的关键差异：
- 认证走查询参数 ``access_token=xxx``（v5 不支持 Authorization 头），故每次请求合并
  ``self._params()``；
- 行级评论用 **patch 内的 position**（该文件 diff 的第几行，1 起），而非 GitHub 的
  side/line 锚点——由 `_position_for()` 从 unified patch 推算；
- repo_id = "owner/repo"（同 GitHub 口径）。

webhook 侧 Gitee 的来源识别/签名校验已在 `forges/signatures.py` 就绪（X-Gitee-Event /
X-Gitee-Token + X-Gitee-Timestamp 的 HMAC-SHA256），本模块只做事件解析与 REST 拉取回写。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import Any

import httpx

from codereview_ai.domain.models import ChangeType, FileDiff, PullRequest
from codereview_ai.forges.base import (
    ForgeAdapter,
    new_file_content_from_patch as _new_file_content_from_patch,
    repo_path_from_url,
)
from codereview_ai.forges.signatures import GITEE

#: 空 files 数组时指数退避的初始/最大延时（秒），与 GitHub/GitLab 一致。
_RETRY_DELAY_0 = 0.5
_RETRY_ATTEMPTS = 3

#: Gitee files API 的 status → ChangeType（与 GitHub 同名同义）。
_STATUS_TO_CHANGE = {
    "added": ChangeType.NEW_FILE,
    "removed": ChangeType.DELETED_FILE,
    "renamed": ChangeType.RENAMED_FILE,
    "modified": ChangeType.MODIFIED,
    "changed": ChangeType.MODIFIED,
    "copied": ChangeType.MODIFIED,
}

def parse_pull_request_payload(data: dict[str, Any]) -> PullRequest | None:
    """从 Gitee `Pull Request` webhook payload 解析中立 PullRequest；非 PR 事件返回 None。"""
    event = data.get("pull_request") or {}
    if not isinstance(event, dict) or not event.get("number"):
        return None
    repo = data.get("repository") or {}
    if not isinstance(repo, dict):
        repo = {}
    full_name = str(repo.get("full_name") or repo.get("path") or "")
    head = event.get("head") or {}
    base = event.get("base") or {}
    if not isinstance(head, dict):
        head = {}
    if not isinstance(base, dict):
        base = {}

    return PullRequest(
        provider=GITEE,
        repo_id=full_name,  # Gitee 项目唯一 id = "owner/repo"（与 GitHub 同口径）
        repo_full_name=full_name,
        web_url=str(event.get("html_url") or repo.get("html_url") or ""),
        pr_number=int(event.get("number") or 0),
        title=str(event.get("title") or ""),
        source_branch=str(head.get("ref") or ""),
        target_branch=str(base.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        base_sha=str(base.get("sha") or ""),
        author=str(((event.get("user") or {}).get("login"))
                   or ((data.get("sender") or {}).get("login")) or ""),
        is_draft=bool(event.get("draft")),
    )


def _to_file_diff(item: dict[str, Any]) -> FileDiff:
    """从 files API 项构造 FileDiff；新侧全文复用 base 的 unified patch 还原。"""
    new_path = str(item.get("filename") or "")
    change = _STATUS_TO_CHANGE.get(str(item.get("status") or ""), ChangeType.MODIFIED)
    old_path = str(item.get("previous_filename") or new_path)
    if change is ChangeType.DELETED_FILE:
        new_path = ""  # 删除文件没有新路径，行级评论挂旧文件名
    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        diff=str(item.get("patch") or ""),
        additions=int(item.get("additions") or 0),
        deletions=int(item.get("deletions") or 0),
        change_type=change,
        new_file_content=_new_file_content_from_patch(item.get("patch") or "", change),
    )


def pull_request_from_item(item: dict[str, Any], repo_id: str = "") -> PullRequest | None:
    """从 Gitee `GET /pulls` 列表项构造中立 PullRequest（无 webhook 层嵌套）。"""
    if not isinstance(item, dict) or not item.get("number"):
        return None
    base = item.get("base") or {}
    head = item.get("head") or {}
    if not isinstance(base, dict):
        base = {}
    if not isinstance(head, dict):
        head = {}
    full_name = str(((base.get("repo") or {}) or {}).get("full_name") or "") or repo_id
    return PullRequest(
        provider=GITEE,
        repo_id=full_name or repo_id,
        repo_full_name=full_name,
        web_url=str(item.get("html_url") or ""),
        pr_number=int(item.get("number") or 0),
        title=str(item.get("title") or ""),
        source_branch=str(head.get("ref") or ""),
        target_branch=str(base.get("ref") or ""),
        head_sha=str(head.get("sha") or ""),
        base_sha=str(base.get("sha") or ""),
        author=str(((item.get("user") or {}).get("login")) or ""),
        is_draft=bool(item.get("draft")),
    )


def _owner_repo(repo_id: str) -> tuple[str, str]:
    """repo_id="owner/repo" → (owner, repo)。"""
    parts = str(repo_id).split("/")
    owner = parts[0] if parts else ""
    repo = parts[1] if len(parts) > 1 else ""
    return owner, repo


def _position_for(patch: str, *, side: str, line: int | None) -> int | None:
    """把 (side, line) 锚点换算成 Gitee 行级评论要的 ``position``。

    position = 目标行在**该文件完整 patch** 中的序号（1 起，`---`/`+++` 元头也算行）。
    遍历 patch：hunk 头刷新两侧起始行号，`+` 行推进新侧行号、`-` 行推进旧侧、上下文
    两侧都推进；命中目标行即返回当前序号。找不到返回 None（该行不投）。
    """
    if line is None:
        return None
    old_no = new_no = -1
    for idx, raw in enumerate(patch.splitlines(), start=1):
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(2))
            continue
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            if side != "LEFT" and new_no == line:
                return idx
            new_no += 1
        elif raw.startswith("-"):
            if side == "LEFT" and old_no == line:
                return idx
            old_no += 1
        else:  # 上下文行
            if (side == "LEFT" and old_no == line) or (side != "LEFT" and new_no == line):
                return idx
            old_no += 1
            new_no += 1
    return None


class GiteeForge(ForgeAdapter):
    """Gitee 平台适配器：围绕注入的 httpx client 实现 ForgeAdapter（API v5）。"""

    name = GITEE

    def __init__(self, api_base: str, token: str, http: httpx.AsyncClient) -> None:
        self._base = str(api_base).rstrip("/")
        self._token = token
        self._http = http

    # ── payload → 中立模型 ──────────────────────────────────────────────
    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        return parse_pull_request_payload(data)

    # ── REST 拉取 ──────────────────────────────────────────────────────
    async def fetch_pull_request(self, pr: PullRequest) -> PullRequest:
        """GET /pulls/{n} 补齐权威 head_sha/base_sha/title（行级评论 commit_id 必填）。"""
        owner, repo = _owner_repo(pr.repo_id)
        resp = await self._http.get(
            f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}",
            params=self._params(),
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

    async def list_pulls(
        self, repo_id: str, *, include_closed: bool = False
    ) -> list[PullRequest]:
        """主动补拉：GET /repos/{owner}/{repo}/pulls?state=...（DESIGN §9 补拉通道）。

        `include_closed=False` → state=open 仅打开态；`True` → state=all 含已关闭/已合并。
        已审过的同 head 由增量决策短路，天然幂等。
        """
        owner, repo = _owner_repo(repo_id)
        if not owner or not repo:
            return []
        state = "all" if include_closed else "open"
        resp = await self._http.get(
            f"{self._base}/repos/{owner}/{repo}/pulls",
            params=self._params(state=state, per_page=100),
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            return []
        return [item for pr in body if (item := pull_request_from_item(pr, repo_id)) is not None]

    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        """GET /pulls/{n}/files；空数组时指数退避重试（与 GitHub/GitLab 一致）。"""
        owner, repo = _owner_repo(pr.repo_id)
        path = f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/files"
        delay = _RETRY_DELAY_0
        for attempt in range(_RETRY_ATTEMPTS):
            resp = await self._http.get(path, params=self._params())
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, list) and body:
                return [_to_file_diff(i) for i in body if isinstance(i, dict)]
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        return []

    # ── 回写评论 ───────────────────────────────────────────────────────
    async def post_summary(self, pr: PullRequest, body: str) -> None:
        """整体总结：POST /pulls/{n}/comments（PR 级评论，字段 body）。"""
        owner, repo = _owner_repo(pr.repo_id)
        resp = await self._http.post(
            f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/comments",
            params=self._params(),
            json={"body": body, "access_token": self._token},
        )
        resp.raise_for_status()

    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        """逐条行级评论：POST /pulls/{n}/comments 带 path + position + commit_id。

        上层锚点是 path/line/side（与 GitHub 同契约），此处换算成 Gitee 的 patch 内
        position；换算不到（行不在 diff 中）跳过该条。Gitee 无批量 review API，逐条发。
        """
        owner, repo = _owner_repo(pr.repo_id)
        if not pr.head_sha:
            return
        patch_by_path: dict[str, str] = {}
        for f in await self.fetch_files(pr):
            if f.new_path:
                patch_by_path[f.new_path] = f.diff
        for c in comments:
            path = str(c.get("path") or "")
            side = str(c.get("side") or "RIGHT")
            line = c.get("old_line") if side == "LEFT" else c.get("line")
            patch = patch_by_path.get(path)
            if not path or patch is None:
                continue
            position = _position_for(patch, side=side, line=int(line) if line else None)
            if position is None:
                continue
            resp = await self._http.post(
                f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/comments",
                params=self._params(),
                json={
                    "body": str(c.get("body") or ""),
                    "commit_id": pr.head_sha,
                    "path": path,
                    "position": position,
                    "access_token": self._token,
                },
            )
            resp.raise_for_status()

    async def list_comments(self, pr: PullRequest) -> list[str]:
        """列出 PR 上已存在评论正文（行级 + PR 级共用一个端点），供幂等去重。"""
        owner, repo = _owner_repo(pr.repo_id)
        try:
            resp = await self._http.get(
                f"{self._base}/repos/{owner}/{repo}/pulls/{pr.pr_number}/comments",
                params=self._params(),
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return []  # 拉不到（如权限）不阻断幂等检查，降级为照发
        return [
            str(item["body"])
            for item in resp.json()
            if isinstance(item, dict) and item.get("body")
        ]

    async def resolve_repo_meta(self, url: str) -> dict[str, str] | None:
        """Gitee 的 repo_id = repo_full_name = "owner/repo"，纯解析 URL 即可，无需凭据。"""
        path = repo_path_from_url(url, GITEE)
        if "/" not in path:
            return None
        return {"repo_id": path, "repo_full_name": path, "web_url": url.strip().rstrip("/")}

    def _params(self, **extra: Any) -> dict[str, Any]:
        """Gitee v5 用查询参数认证；每次请求合并 access_token 与分页/过滤参数。"""
        params: dict[str, Any] = {"access_token": self._token}
        params.update(extra)
        return params

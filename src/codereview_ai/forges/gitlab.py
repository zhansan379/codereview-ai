"""GitLab 适配器：merge_request payload → PullRequest；changes API → FileDiff（DESIGN §9）。

字段路径对照 `reference/platform_payload_map.md` §3/§4。
- webhook 的 `object_attributes.last_commit.id` 是幂等键 head_sha。
- changes API 响应里的数组元素自带 unified diff，直接转换；`access_raw_diffs=true`
  让服务端返回原始 unified diff 而非分解后的 JSON。
- GitLab 在 MR 刚创建时 changes API 可能返回**空数组**（服务端还在算 diff），
  这里用 `asyncio.sleep` + 指数退避重试（不对，照 reference 用的是 `time.sleep`，
  但那是阻塞 worker 的反模式——见 antipatterns，这里必须用协程等待）。
- 发起网络调用的 HTTP 客户端在构造时注入（测试用 httpx.MockTransport，离线可测）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import httpx

from codereview_ai.domain.models import FileDiff, PullRequest
from codereview_ai.forges.base import ForgeAdapter, change_type_from_flags, count_diff_stats
from codereview_ai.forges.signatures import GITLAB

#: 空 changes 数组时指数退避的初始/最大延时（秒）。
_RETRY_DELAY_0 = 0.5
_RETRY_ATTEMPTS = 3


def parse_merge_request_payload(data: dict[str, Any]) -> PullRequest | None:
    """从 GitLab `merge_request` webhook payload 解析中立 PullRequest。

    非 merge_request 事件返回 None。
    """
    if data.get("object_kind") != "merge_request":
        return None
    oa = data.get("object_attributes") or {}
    if not isinstance(oa, dict) or not oa.get("iid"):
        return None
    project = data.get("project") or {}
    if not isinstance(project, dict):
        project = {}
    last_commit = oa.get("last_commit") or {}
    if not isinstance(last_commit, dict):
        last_commit = {}

    return PullRequest(
        provider=GITLAB,
        repo_id=str(oa.get("target_project_id") or project.get("id") or ""),
        repo_full_name=str(project.get("path_with_namespace") or project.get("name") or ""),
        web_url=str(oa.get("url") or ""),
        pr_number=int(oa.get("iid") or 0),
        title=str(oa.get("title") or ""),
        source_branch=str(oa.get("source_branch") or ""),
        target_branch=str(oa.get("target_branch") or ""),
        head_sha=str(last_commit.get("id") or ""),
        base_sha="",  # webhook 里不一定有，交给 fetch_pull_request 从 API 补齐
        diff_refs=None,
        author=str(((data.get("user") or {}) or {}).get("username") or ""),
    )


def _to_file_diff(item: dict[str, Any]) -> FileDiff:
    old_path = str(item.get("old_path") or "")
    new_path = str(item.get("new_path") or old_path)
    diff = str(item.get("diff") or "")
    adds, dels = count_diff_stats(diff)
    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        diff=diff,
        additions=adds,
        deletions=dels,
        change_type=change_type_from_flags(
            is_new=bool(item.get("new_file")),
            is_deleted=bool(item.get("deleted_file")),
            is_renamed=bool(item.get("renamed_file")),
        ),
        # GitLab changes API 不返回文件全文；全文兜底留给后续按文件补抓（M3）。
        new_file_content="",
    )


class GitLabForge(ForgeAdapter):
    """GitLab 平台适配器：围绕注入的 httpx client 实现的 ForgeAdapter。"""

    name = GITLAB

    def __init__(self, api_base: str, token: str, http: httpx.AsyncClient) -> None:
        self._base = str(api_base).rstrip("/")
        self._token = token
        self._http = http

    # ── payload → 中立模型 ──────────────────────────────────────────────
    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        return parse_merge_request_payload(data)

    # ── REST 拉取 ──────────────────────────────────────────────────────
    async def fetch_pull_request(self, pr: PullRequest) -> PullRequest:
        """GET /merge_requests/{iid} 补齐 diff_refs（回写评论 position 必填）。"""
        resp = await self._http.get(
            f"{self._base}/api/v4/projects/{pr.repo_id}/merge_requests/{pr.pr_number}",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        body = resp.json()
        refs = body.get("diff_refs") if isinstance(body, dict) else None
        if isinstance(refs, dict):
            return replace(
                pr,
                diff_refs={
                    "base_sha": refs.get("base_sha") or "",
                    "head_sha": refs.get("head_sha") or "",
                    "start_sha": refs.get("start_sha") or "",
                },
            )
        return pr

    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        """GET /merge_requests/{iid}/changes；空 changes 时指数退避重试。"""
        path = (
            f"{self._base}/api/v4/projects/{pr.repo_id}/merge_requests/{pr.pr_number}"
            "/changes?access_raw_diffs=true"
        )
        delay = _RETRY_DELAY_0
        for attempt in range(_RETRY_ATTEMPTS):
            resp = await self._http.get(path, headers=self._auth_headers())
            resp.raise_for_status()
            body = resp.json()
            changes = body.get("changes") if isinstance(body, dict) else None
            if changes:
                return [_to_file_diff(c) for c in changes if isinstance(c, dict)]
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(delay)  # 协程等待，不阻塞事件循环
                delay *= 2
        return []

    # ── 回写评论 ───────────────────────────────────────────────────────
    async def post_summary(self, pr: PullRequest, body: str) -> None:
        """整体总结：POST /merge_requests/{iid}/notes，字段名 body。"""
        resp = await self._http.post(
            f"{self._base}/api/v4/projects/{pr.repo_id}/merge_requests/{pr.pr_number}/notes",
            headers=self._auth_headers(),
            json={"body": body},
        )
        resp.raise_for_status()

    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        """逐条行级评论：POST /merge_requests/{iid}/discussions，带 position。

        comments 元素已带 old_line/new_line（由上层按 side 选一，勿同时给）；
        缺失 diff_refs 或行号时不投（避免 400）。
        """
        refs = pr.diff_refs or {}
        if not (refs.get("base_sha") and refs.get("head_sha")):
            return
        for c in comments:
            line_key, value = ("old_line", c.get("old_line")) if c.get("side") == "LEFT" else (
                "new_line",
                c.get("line"),
            )
            if not value:
                continue
            position = {
                "position_type": "text",
                "base_sha": refs.get("base_sha"),
                "head_sha": refs.get("head_sha"),
                "start_sha": refs.get("start_sha") or refs.get("base_sha"),
                "new_path": c.get("path") or "",
                "old_path": c.get("old_path") or c.get("path") or "",
                line_key: int(value),
            }
            resp = await self._http.post(
                f"{self._base}/api/v4/projects/{pr.repo_id}/merge_requests/{pr.pr_number}/discussions",
                headers=self._auth_headers(),
                json={"body": str(c.get("body") or ""), "position": position},
            )
            resp.raise_for_status()

    def _auth_headers(self) -> dict[str, str]:
        return {"Private-Token": self._token}

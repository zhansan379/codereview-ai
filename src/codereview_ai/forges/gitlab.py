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
import urllib.parse
from dataclasses import replace
from typing import Any

import httpx

from codereview_ai.domain.models import CommitInfo, FileDiff, PullRequest, PushEvent
from codereview_ai.forges.base import (
    ForgeAdapter,
    change_type_from_flags,
    count_diff_stats,
    new_file_content_from_patch,
    repo_path_from_url,
)
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


def parse_push_event_payload(data: dict[str, Any]) -> PushEvent | None:
    """从 GitLab `push` webhook payload 解析中立 PushEvent（§7.7）。

    非 push 事件返回 None；`ref` 需截取 `refs/heads/` 前缀。
    """
    if data.get("object_kind") != "push":
        return None
    project = data.get("project") or {}
    if not isinstance(project, dict):
        project = {}
    repo_id = str(project.get("id") or "")
    if not repo_id:
        return None
    ref = str(data.get("ref") or "")
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    commits = data.get("commits") or []
    return PushEvent(
        provider=GITLAB,
        repo_id=repo_id,
        repo_full_name=str(project.get("path_with_namespace") or ""),
        branch=branch,
        before=str(data.get("before") or ""),
        after=str(data.get("after") or ""),
        commits=[
            CommitInfo(
                sha=str(c.get("id") or ""),
                message=str(c.get("message") or ""),
                author_name=str(((c.get("author") or {}) or {}).get("name") or ""),
            )
            for c in commits if isinstance(c, dict)
        ],
        pusher=str(data.get("user_username") or ""),
    )


def _to_file_diff(item: dict[str, Any]) -> FileDiff:
    old_path = str(item.get("old_path") or "")
    new_path = str(item.get("new_path") or old_path)
    diff = str(item.get("diff") or "")
    adds, dels = count_diff_stats(diff)
    change_type = change_type_from_flags(
        is_new=bool(item.get("new_file")),
        is_deleted=bool(item.get("deleted_file")),
        is_renamed=bool(item.get("renamed_file")),
    )
    return FileDiff(
        old_path=old_path,
        new_path=new_path,
        diff=diff,
        additions=adds,
        deletions=dels,
        change_type=change_type,
        # changes API 的 diff 是完整 unified patch，可直接还原新侧全文，
        # 供覆盖集/未变更复用判定（修改文件此前恒空导致覆盖集失效，已修复）。
        new_file_content=new_file_content_from_patch(diff, change_type),
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

    def parse_push_event(self, data: dict[str, Any]) -> PushEvent | None:
        return parse_push_event_payload(data)

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

    async def list_pulls(
        self, repo_id: str, *, include_closed: bool = False
    ) -> list[PullRequest]:
        """主动补拉：GET /projects/{id}/merge_requests?state=... 列出 MR（DESIGN §9）。

        `include_closed=False` → `state=opened` 仅打开态；`True` → `state=all` 含已关闭/已合并。
        逐项归一成中立 PullRequest；`diff_refs` 一并填入（GitLab 行级评论 position 必填，
        见 `post_inline`）。`sha` 即 head_sha（gitlab 列表项无独立 base_sha，由补拉后的
        `fetch_pull_request` 补齐 diff_refs 即可）。
        """
        if not repo_id:
            return []
        state = "all" if include_closed else "opened"
        resp = await self._http.get(
            f"{self._base}/api/v4/projects/{repo_id}/merge_requests"
            f"?state={state}&scope=all&per_page=100",
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            return []
        out: list[PullRequest] = []
        for item in body:
            if not isinstance(item, dict) or not item.get("iid"):
                continue
            refs = item.get("diff_refs")
            out.append(PullRequest(
                provider=GITLAB,
                repo_id=repo_id,
                repo_full_name="",  # 列表项无项目路径；由上层按 Project.repo_full_name 补齐
                web_url=str(item.get("web_url") or ""),
                pr_number=int(item.get("iid") or 0),
                title=str(item.get("title") or ""),
                source_branch=str(item.get("source_branch") or ""),
                target_branch=str(item.get("target_branch") or ""),
                head_sha=str(item.get("sha") or ""),
                base_sha="",  # 列表项无 base.sha，fetch_pull_request 补齐 diff_refs
                diff_refs=(
                    {
                        "base_sha": refs.get("base_sha") or "",
                        "head_sha": refs.get("head_sha") or "",
                        "start_sha": refs.get("start_sha") or "",
                    }
                    if isinstance(refs, dict) else None
                ),
                author=str(((item.get("author") or {}) or {}).get("username") or ""),
            ))
        return out

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

    async def list_comments(self, pr: PullRequest) -> list[str]:
        """列出 MR 上已存在评论正文（notes），供幂等去重。"""
        try:
            resp = await self._http.get(
                f"{self._base}/api/v4/projects/{pr.repo_id}/merge_requests/{pr.pr_number}/notes",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return []  # 拉不到不阻断幂等检查，降级为照发
        return [
            str(item["body"]) for item in resp.json()
            if isinstance(item, dict) and item.get("body")
        ]

    # ── push 轨（§7.7）：compare / 单 commit diff / head commit 总结回写 ──
    async def _get_diffs_with_retry(self, path: str) -> list[FileDiff]:
        """GET 变化 API 并按 changes/diffs 数组转换；空数组指数退避重试。"""
        delay = _RETRY_DELAY_0
        for attempt in range(_RETRY_ATTEMPTS):
            resp = await self._http.get(path, headers=self._auth_headers())
            resp.raise_for_status()
            body = resp.json()
            raw = body.get("diffs") if isinstance(body, dict) else None
            if isinstance(raw, list) and raw:
                return [_to_file_diff(d) for d in raw if isinstance(d, dict)]
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        return []

    async def get_push_changes(self, ev: PushEvent) -> list[FileDiff]:
        """compare from=before&to=after；after 全 0（删分支）直接返回空。"""
        if ev.after and ev.after.count("0") == len(ev.after):
            return []
        path = (
            f"{self._base}/api/v4/projects/{ev.repo_id}/repository/compare"
            f"?from={ev.before}&to={ev.after}"
        )
        return await self._get_diffs_with_retry(path)

    async def get_first_commit_changes(self, ev: PushEvent) -> list[FileDiff]:
        """新分支（before 全 0）：单 commit diff API 拉首个提交差量。"""
        if not ev.commits:
            return []
        sha = ev.commits[0].sha
        path = f"{self._base}/api/v4/projects/{ev.repo_id}/repository/commits/{sha}/diff"
        delay = _RETRY_DELAY_0
        for attempt in range(_RETRY_ATTEMPTS):
            resp = await self._http.get(path, headers=self._auth_headers())
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, list) and body:
                return [_to_file_diff(d) for d in body if isinstance(d, dict)]
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        return []

    async def post_commit_summary(self, ev: PushEvent, text: str) -> None:
        """总结回写到 head commit：POST commits/{sha}/comments，字段 **note**（§13.1）。"""
        resp = await self._http.post(
            f"{self._base}/api/v4/projects/{ev.repo_id}/repository/commits/{ev.after}/comments",
            headers=self._auth_headers(),
            json={"note": text},
        )
        resp.raise_for_status()

    async def post_commit_status(
        self, pr: PullRequest, *, passed: bool, description: str = ""
    ) -> None:
        """F3.7：set head commit 的 CI status（POST statuses/{sha}）。"""
        if not pr.head_sha:
            return
        resp = await self._http.post(
            f"{self._base}/api/v4/projects/{pr.repo_id}/statuses/{pr.head_sha}",
            headers=self._auth_headers(),
            json={
                "state": "success" if passed else "failed",
                "name": "codereview-ai",
                "description": description,
            },
        )
        resp.raise_for_status()

    async def fetch_project(self, path: str) -> dict[str, Any] | None:
        """按 ``namespace/path``（可嵌套）查项目元数据，返回 GitLab API 的项目对象。

        GitLab 的 `:id` 接受 URL-encode 后的路径（``/``→``%2F``）；404 返回 None。
        """
        enc = urllib.parse.quote(path, safe="")
        resp = await self._http.get(
            f"{self._base}/api/v4/projects/{enc}",
            headers=self._auth_headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def resolve_repo_meta(self, url: str) -> dict[str, str] | None:
        """GitLab 的 repo_id 是**数字项目 ID**（不是 URL 路径），须在线查 API 换回。

        从 URL 解析出 namespace/path（可含子组），调 ``/projects/{path}`` 拿
        `id` 与 `path_with_namespace`，作为项目自动补全的 repo_id / repo_full_name。
        """
        path = repo_path_from_url(url, GITLAB)
        if not path:
            return None
        proj = await self.fetch_project(path)
        if not proj:
            return None
        return {
            "repo_id": str(proj.get("id") or "").strip(),
            "repo_full_name": str(proj.get("path_with_namespace") or path),
            "web_url": str(proj.get("web_url") or url.rstrip("/")),
        }

    def _auth_headers(self) -> dict[str, str]:
        return {"Private-Token": self._token}

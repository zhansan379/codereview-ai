"""ForgeAdapter 抽象：平台无关的 PR 解析 + 变更/评论回写接口（DESIGN §9）。

实现者把各平台 Webhook payload / REST API 归一成中立领域模型
（PullRequest / FileDiff / ChangeType），上层（worker 审查编排）只依赖本接口。
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any

from codereview_ai.domain.models import ChangeType, FileDiff, PullRequest, PushEvent
from codereview_ai.forges.signatures import GITEA, GITEE, GITHUB, GITLAB

logger = logging.getLogger("codereview_ai.forges.base")

#: 触发审查的事件动作白名单（open/update 语义，跨平台归一）。
REVIEW_ACTIONS = frozenset({"open", "opened", "reopen", "reopened", "update", "synchronize"})

#: URL 中跟在仓库路径之后的「动作段」——解析仓库路径时在此截断（GitLab 尤需）。
_ACTION_SEGS = frozenset(
    {"-", "-/", "merge_requests", "issues", "commits", "tree", "blob",
     "pipeline", "pipelines", "wiki", "wikis", "releases", "settings", "forks"}
)

#: unified diff hunk 头，取「新侧起始行号」（第二个 `+\d+`）。
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def new_file_content_from_patch(patch: str, change: ChangeType) -> str:
    """从平台返回的单个文件 unified patch 还原**新侧**全文；删除文件 → ""。

    每个文件的 patch 是从头到尾的完整 diff：每一行要么在某个 `@@ ... @@` hunk 内、
    要么是 `--- / +++` 元头或 `\\ No newline` 标记。按 hunk 头携带的**新侧起始行号**，
    把 `+`（新增）与 ` `（上下文）行铺回对应行号、`-`（删除）行剔除，即得该文件在新
    head 的完整正文。对 NEW_FILE 退化为「剥 `+` 前缀」。

    注意「完整」仅指 patch 自身：unified diff 只带变更点附近几行上下文，未被 hunk
    覆盖的行在这里被填成**空行**。故产物适合变更区周边的场景（覆盖集、未变更复用、
    行号定位），不适合整体解析——静态分析跑 ruff 前必须先 `enrich_new_file_contents`
    换成平台真实全文（#62 报告 80+ 假 finding 的教训），本函数结果仅作富化失败时的兜底。
    """
    if change is ChangeType.DELETED_FILE:
        return ""
    modelines: dict[int, str] = {}
    max_line = 0
    new_no: int | None = None
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            new_no = int(m.group(1)) if m else None
            continue
        if raw.startswith("\\"):  # “\ No newline at end of file”
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+") or raw.startswith(" "):
            if new_no is None:
                continue
            modelines[new_no] = raw[1:]
            max_line = max(max_line, new_no)
            new_no += 1
    if not modelines:
        return ""
    return "\n".join(modelines.get(i, "") for i in range(1, max_line + 1))


async def enrich_new_file_contents(
    forge: ForgeAdapter,
    diffs: list[FileDiff],
    repo_id: str,
    ref: str,
    *,
    concurrency: int = 6,
) -> list[FileDiff]:
    """把 `FileDiff.new_file_content` 从 patch 重建升级为平台真实全文（静态分析前置）。

    patch 重建对未被 hunk 覆盖的行一律填空行（unified diff 只带变更点附近几行上下文），
    拿它跑 ruff 会把断掉的字符串/注释当语法错误雪崩误报（报告 #62 一次性 80+ 条假
    finding 的根因）。这里按文件**并发**调 `forge.fetch_file_content` 拉真实全文覆盖；
    单文件失败/不支持（返回空）/拉空 → 保留 patch 重建内容（行号定位、未变更复用等
    仍可用其变更区）。删除文件无新侧，跳过；`ref` 缺失原样返回。绝不抛异常：富化只是
    增强，失败只能降级，绝不阻断审查主链。
    """
    if not ref:
        return diffs
    targets = [
        d for d in diffs
        if d.change_type is not ChangeType.DELETED_FILE
        and d.new_path and d.new_path != "/dev/null"
    ]
    paths = sorted({d.new_path for d in targets})
    if not paths:
        return diffs
    sem = asyncio.Semaphore(concurrency)

    async def _one(path: str) -> tuple[str, str]:
        async with sem:
            return path, await forge.fetch_file_content(repo_id, path, ref)

    results = await asyncio.gather(*(_one(p) for p in paths), return_exceptions=True)
    by_path: dict[str, str] = {}
    failures = 0
    for r in results:
        if isinstance(r, BaseException):
            failures += 1  # 限流/权限/路径异常 → 该文件留用 patch 重建
            logger.debug("真文拉取失败：%r", r)
            continue
        path, content = r
        if content:
            by_path[path] = content
    if failures:
        logger.warning("真文拉取 %d/%d 个文件失败，这些文件留用 patch 重建内容",
                       failures, len(paths))
    if not by_path:
        return diffs
    out = [
        replace(d, new_file_content=by_path[d.new_path]) if d.new_path in by_path else d
        for d in diffs
    ]
    logger.info("真文富化：%d/%d 个文件已用平台 API 全文覆盖 patch 重建",
                len(by_path), len(paths))
    return out


#: 公开托管站 → 平台（新增项目从 URL 免选平台的识别依据）。
_PUBLIC_HOST_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("github.com", GITHUB),
    ("gitlab.com", GITLAB),
    ("gitee.com", GITEE),
)

#: 自建站常见命名子串 → 平台（如 gitlab.corp.com / gitea.example.cn）。
_HOST_KEYWORD_PROVIDERS: tuple[tuple[str, str], ...] = (
    (GITHUB, GITHUB),
    (GITLAB, GITLAB),
    (GITEA, GITEA),
    (GITEE, GITEE),
)


def provider_from_url_host(url: str) -> str:
    """从仓库链接的 host 识别平台，供「新增项目」免手动选平台。

    命中顺序：公开托管站精确后缀（github.com / gitlab.com / gitee.com）→ 自建常见
    命名子串（host 含 github/gitlab/gitea/gitee）。识别不了返回 ``""``，由调用方
    再走特征路径探测（`detect_provider_by_probe`）或让用户手动选平台。
    """
    try:
        netloc = urllib.parse.urlparse(url.strip()).netloc
    except (TypeError, ValueError):
        return ""
    # 去掉 user:pass@ 与端口，只留主机名
    host = netloc.rsplit("@", 1)[-1].rsplit(":", 1)[0].lower().rstrip(".")
    if not host:
        return ""
    for suffix, provider in _PUBLIC_HOST_PROVIDERS:
        if host == suffix or host.endswith(f".{suffix}"):
            return provider
    for keyword, provider in _HOST_KEYWORD_PROVIDERS:
        if keyword in host:
            return provider
    return ""


def repo_path_from_url(url: str, provider: str = "") -> str:
    """从仓库链接解析出规范 ``owner/repo``（GitHub 系）或 ``namespace/path``（GitLab 嵌套）。

    - 去掉协议/host、尾部 ``.git`` 与斜杠；
    - GitHub/Gitea/Gitee 只取路径前两段（owner/repo）并忽略其后资源段；
    - GitLab 保留嵌套子组路径，截到动作段（``-`` 或 merge_requests/issues/...）之前。
    无法解析（缺 host/path 或不是仓库链接）返回 ``""``。
    """
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except (TypeError, ValueError):
        return ""
    if not parsed.netloc or not parsed.path:
        return ""
    segs = [s for s in parsed.path.split("/") if s]
    if not segs:
        return ""
    if segs[-1].endswith(".git"):
        segs[-1] = segs[-1][:-4]
    segs = [s for s in segs if s]
    if str(provider).lower() == "gitlab":
        for i, s in enumerate(segs):
            if s in _ACTION_SEGS:
                segs = segs[:i]
                break
    else:
        segs = segs[:2]
    path = "/".join(segs)
    return path if "/" in path else ""


def change_type_from_flags(*, is_new: bool, is_deleted: bool, is_renamed: bool) -> ChangeType:
    if is_deleted:
        return ChangeType.DELETED_FILE
    if is_new:
        return ChangeType.NEW_FILE
    if is_renamed:
        return ChangeType.RENAMED_FILE
    return ChangeType.MODIFIED


def count_diff_stats(diff: str) -> tuple[int, int]:
    """从 unified diff 文本粗略统计 (additions, deletions)——供过滤/展示用。"""
    adds = dels = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


class ForgeAdapter(ABC):
    """平台适配器。HTTP 客户端在构造时注入，测试可用 httpx.MockTransport。"""

    name: str = ""

    @abstractmethod
    def parse_merge_request(self, data: dict[str, Any]) -> PullRequest | None:
        """从 webhook 的 merge_request 事件解析出中立 PullRequest。"""

    @staticmethod
    def should_review(action: str) -> bool:
        return action in REVIEW_ACTIONS

    async def fetch_pull_request(self, pr: PullRequest) -> PullRequest:
        """按需补齐 PR 元数据（diff_refs / 标题 / 作者），默认原样返回。"""
        return pr

    async def list_pulls(self, repo_id: str, *, include_closed: bool = False) -> list[PullRequest]:
        """主动补拉：列出仓库的 PR/MR（不依赖 webhook，DESIGN §9 补拉通道）。

        `include_closed=False`（默认）仅列出**打开**态；`True` 时同时含**已关闭/已合并**
        （由前端设置页「补拉范围」开关控制）。非抽象默认返回空——未实现此能力（如推送轨
        专用的分析/测试子类）直接留白，补拉对该仓库自然跳过而非报错。实现者用 `repo_id`
        定位仓库并逐项归一成中立 `PullRequest`（字段口径与 `parse_merge_request` 一致，
        含 head_sha/base_sha/diff_refs——GitLab 行级评论 position 依赖后者）。
        """
        return []

    @abstractmethod
    async def fetch_files(self, pr: PullRequest) -> list[FileDiff]:
        """拉取 PR 涉及文件的 diff（含 full new_file_content 则更好）。"""

    async def fetch_file_content(self, repo_id: str, path: str, ref: str) -> str:
        """拉取 `ref` 上某文件的**真实全文**（`enrich_new_file_contents` 的单文件原语）。

        非抽象默认返回空串：未实现此能力的平台/极简测试桩 → 富化自动跳过，各文件
        留用 patch 重建内容（与 list_comments 的降级语义一致）。拉不到也返回空串；
        抛异常同样可以，上层按单文件失败吞掉降级。
        """
        return ""

    @abstractmethod
    async def post_summary(self, pr: PullRequest, body: str) -> None:
        """发总结评论。"""

    @abstractmethod
    async def post_inline(self, pr: PullRequest, comments: list[dict[str, Any]]) -> None:
        """并行发行级评论。"""

    async def list_comments(self, pr: PullRequest) -> list[str]:
        """列出 PR 上已存在的评论正文（幂等去重用）。

        非抽象默认返回空：未实现列表能力（如测试/分析子类）时幂等检查降级为"从未投递过"，
        重发会照发。实现者按平台取行级评论 + 总结评论两类正文合并返回。
        """
        return []

    async def list_inline_anchors(self, pr: PullRequest) -> set[tuple[str, int, str, str]]:
        """已投行级评论的锚点集合 (path, line, side, body)，分批回写的补发去重用。

        非抽象默认返回空：不支持的平台/极简测试桩返回空集，上层跳过锚点去重（照发，
        与 list_comments 的降级语义一致）。分批回写中途失败后重发，已落地批次靠这里
        跳过——指纹 sentinel 在总结评论末尾，行级未投完前它不会出现。
        """
        return set()

    # ── push 轨（§7.7）：非抽象默认，未实现的分析/测试子类可只保 MR 轨 ──
    def parse_push_event(self, data: dict[str, Any]) -> PushEvent | None:
        """从 webhook 的 push 事件解析出中立 PushEvent；不支持/非 push 返回 None。"""
        return None

    async def get_push_changes(self, ev: PushEvent) -> list[FileDiff]:
        """compare 差量（before→after）。"""
        raise NotImplementedError

    async def get_first_commit_changes(self, ev: PushEvent) -> list[FileDiff]:
        """新分支：only 首个提交的差量。"""
        raise NotImplementedError

    async def post_commit_summary(self, ev: PushEvent, text: str) -> None:
        """push 总结回写到 head commit（MR 轨用 post_summary，push 无 MR 可挂）。"""
        raise NotImplementedError

    async def post_commit_status(
        self, pr: PullRequest, *, passed: bool, description: str = ""
    ) -> None:
        """设一条 head commit 的 CI status（F3.7）：低于阈值 blocked 时 passed=False。

        只传语义布尔 `passed`，各平台自行映射成 state（GitLab success/failed，
        GitHub success/failure）。未实现的分析/测试子类可不覆盖。
        """
        raise NotImplementedError

    async def resolve_repo_meta(self, url: str) -> dict[str, str] | None:
        """从仓库链接解析出 {repo_id, repo_full_name, web_url}；无法解析返回 None。

        供「新增项目」从 URL 自动回填 repo_id/repo_full_name。未实现的分析/测试子类
        默认 None（上层按解析失败处理）。
        """
        return None

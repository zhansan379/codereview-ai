"""平台接入能力探测（DESIGN §9 补拉通道的权限盲区诊断）。

「测试连接」不再只回一个 bool，而是按**系统所需能力**逐项探测当前 token 的支持情况，
返回「可用 / 缺权限 / 未知」矩阵供后台展示，让用户一眼看出「能补拉吗 / 能评论吗」。

判定策略：
- **GitHub**：`GET /user` 成功后在响应头读权威的 `X-OAuth-Scopes`，与各能力所需 scope 求交
  （`repo` / `public_repo` / `repo:status` 等）。
  - 头缺失时按**官方 token 前缀**识别凭证类型并精确提示（`github_pat_`=fine-grained、
    `ghs_`=GitHub App、`ghp_`=classic），不再笼统标「可能 fine-grained」；
  - fine-grained PAT 不暴露权限头，额外发 `GET /user/repos` **读探针**判定 read_pull；
    写能力（评论/状态）用**必然失败的写探针**——往不存在的资源（`pulls/0`、`issues/0`、
    非法 SHA）发写请求，`403`=缺权限、`404/422`=有权限，无副作用；
  - 无法识别类型 → 标 `unknown` 诚实不猜；`/user` 非 2xx → 认证本身失败，connect=missing、
    其余 unknown。
- **GitLab**：不再验证 scope（PAT 不从 `/user` 暴露 scope），改为**读探针**——`/user` 判 connect、
  `/projects` 判 read_pull（补拉/拉 files 只要求读权限）；评论/状态是**写**权限，避免做真实写探针
  （有副作用），一律标 `unknown` + 提示需 `api` 权限，诚实降级。

注入 httpx client 便于离线测试（MockTransport），与 forge 适配器同一风格。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from codereview_ai.forges.signatures import GITHUB, GITEE

#: 系统所需能力 → GitHub 任一满足即可的 scope（网络凭证在 HTTP 层判，见 GITHUB_REQUIRED_SCOPES）。
GITHUB_REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    "read_pull": ("repo", "public_repo"),
    "post_comment": ("repo",),
    "commit_status": ("repo", "repo:status"),
}

#: 能力展示标签（前端矩阵用；与 GitHub scope 名解耦）。
CAPABILITY_LABELS: dict[str, str] = {
    "connect": "平台连通 / 认证",
    "read_pull": "拉取打开 PR/MR（补拉 & 审查读取）",
    "post_comment": "发总结 / 行级评论",
    "commit_status": "写 commit 状态",
}

#: 能力稳定顺序（前端矩阵、接口返回都按此排）。
CAPABILITY_ORDER = ("connect", "read_pull", "post_comment", "commit_status")


@dataclass
class Capability:
    """一项能力探测结果；`status` ∈ {ok, missing, unknown}。"""

    name: str
    label: str
    status: str  # ok | missing | unknown
    detail: str = ""


def _cap(name: str, status: str, detail: str = "") -> Capability:
    return Capability(name=name, label=CAPABILITY_LABELS.get(name, name), status=status, detail=detail)


#: GitHub 官方 token 前缀 → 凭证类型（用于 X-OAuth-Scopes 缺失时精确提示，避免误猜）。
GITHUB_TOKEN_PREFIXES: dict[str, tuple[str, ...]] = {
    "fine-grained": ("github_pat_",),
    "github_app": ("ghs_",),
    "oauth_app": ("gho_",),
    "classic": ("ghp_",),
}

#: 未返回 X-OAuth-Scopes 时，按凭证类型给能力列的说明。
_GITHUB_NO_SCOPE_DETAIL: dict[str, str] = {
    "fine-grained": (
        "Fine-grained PAT（github_pat_）：GitHub 不暴露权限头，无法枚举具体权限；"
        "评论/状态需在目标仓库配 Pull requests / Commit statuses 写权限"
    ),
    "github_app": "GitHub App 安装令牌（ghs_）：权限由 App 安装配置决定，未返回 X-OAuth-Scopes",
    "oauth_app": "OAuth App 令牌（gho_）：未返回 X-OAuth-Scopes",
    "classic": "Classic PAT 未返回 X-OAuth-Scopes（异常情况）",
    "unknown": "未返回 X-OAuth-Scopes（可能是 fine-grained PAT / 受限 token）",
}


def _github_token_type(token: str) -> str:
    """按官方 token 前缀识别 GitHub 凭证类型；识别不了返回 unknown。"""
    for kind, prefixes in GITHUB_TOKEN_PREFIXES.items():
        if token.startswith(prefixes):
            return kind
    return "unknown"


def _github_scope_caps(scopes: set[str], conn_ok: bool) -> list[Capability]:
    if not conn_ok:
        return [
            _cap("connect", "missing", "认证失败"),
            *[_cap(n, "unknown", "认证未通过，无法判定") for n in CAPABILITY_ORDER if n != "connect"],
        ]
    out = [_cap("connect", "ok")]
    for name, need in GITHUB_REQUIRED_SCOPES.items():
        have = scopes & set(need)
        if have:
            out.append(_cap(name, "ok"))
        else:
            out.append(_cap(name, "missing", f"缺 scope：{', '.join(need)}"))
    return out


def _github_no_scope_caps(token: str) -> list[Capability]:
    """/user 通但没返回 scope 头：按 token 前缀精确提示，能力仍诚实标 unknown。"""
    ttype = _github_token_type(token)
    detail = _GITHUB_NO_SCOPE_DETAIL.get(ttype, _GITHUB_NO_SCOPE_DETAIL["unknown"])
    conn_detail = "认证通过" if ttype == "unknown" else f"认证通过（{ttype} token）"
    return [
        _cap("connect", "ok", conn_detail),
        *[_cap(n, "unknown", detail) for n in CAPABILITY_ORDER if n != "connect"],
    ]


def _github_probe_status(code: int) -> str:
    """写探针响应码 → {ok, missing, unknown}：403=缺权限，404/422=资源不存在但权限过，其余不明。"""
    if code in (401, 403):
        return "missing"
    if code in (404, 422) or code < 500:
        return "ok"
    return "unknown"


#: 写探针最多试几个仓库（`/user/repos` 首页可能混入 token 未授权的仓库，多试几个更稳）。
_MAX_PROBE_REPOS = 3


def _github_repo_candidates(repos: httpx.Response) -> list[str]:
    """从 `/user/repos` 响应取候选探测仓库（有 push 权限的优先，最多 `_MAX_PROBE_REPOS` 个）。"""
    try:
        body = repos.json()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(body, list):
        return []
    items = [r for r in body if isinstance(r, dict) and r.get("full_name")]
    items.sort(key=lambda r: 0 if (r.get("permissions") or {}).get("push") else 1)
    return [str(r["full_name"]) for r in items[:_MAX_PROBE_REPOS]]


def _github_deny_hint(resp: httpx.Response) -> str:
    """从 403/401 响应提取 GitHub 的权威原因（X-Accepted-GitHub-Permissions 头 + body message）。

    例如「statuses=write」（端点要求的权限）或「Resource not accessible by fine-grained
    personal access token」（token 未覆盖该仓库）——比笼统猜权限更能定位问题。
    """
    header = resp.headers.get("X-Accepted-GitHub-Permissions") or ""
    msg = ""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            msg = str(body["message"])
    except Exception:  # noqa: BLE001
        pass
    if header and msg:
        return f"{header}｜{msg}"
    return header or msg


async def _github_fine_grained_probe(
    client: httpx.AsyncClient, base: str, auth: dict[str, str], repos: httpx.Response
) -> list[Capability]:
    """fine-grained PAT：读探针判 read_pull，必败写探针判评论/状态的写权限（均无副作用）。"""
    connect = _cap("connect", "ok", "认证通过（Fine-grained PAT / github_pat_）")
    candidates = _github_repo_candidates(repos) if repos.status_code < 400 else []

    if repos.status_code >= 400:
        if repos.status_code in (401, 403):
            detail = f"读探针 /user/repos 被拒（HTTP {repos.status_code}）：未授予仓库读权限"
            read = _cap("read_pull", "missing", detail)
        else:
            read = _cap("read_pull", "unknown", f"读探针 /user/repos HTTP {repos.status_code}")
    elif candidates:
        read = _cap("read_pull", "ok", f"读探针通过：可读 {candidates[0]} 等仓库")
    else:
        read = _cap("read_pull", "unknown", "读探针 /user/repos 为空列表：当前未授权任何可读仓库")

    comment, status = await _probe_fine_grained_writes(client, base, candidates, auth)
    return [connect, read, comment, status]


async def _probe_fine_grained_writes(
    client: httpx.AsyncClient, base: str, repos: list[str], auth: dict[str, str]
) -> tuple[Capability, Capability]:
    """对候选仓库发「必然失败」的写探针：资源不存在/非法 SHA → 无副作用。

    总结走 issues/{n}/comments（Issues: write 或 Pull requests: write）、行级走
    pulls/{n}/reviews（Pull requests: write）、评分卡走 statuses/{sha}
    （Commit statuses: write）。任一仓库探到「非 403」即算该能力可用——`/user/repos`
    首页可能混入 token 实际未授权的仓库，多试几个避免误报「缺权限」。
    """
    if not repos:
        return (
            _cap("post_comment", "unknown", "无目标仓库可探测写权限"),
            _cap("commit_status", "unknown", "无目标仓库可探测写权限"),
        )

    issue_ok = issue_denied = False
    review_ok = review_denied = False
    status_ok = status_denied = False
    comment_hint = status_hint = ""

    for repo in repos:
        if not (issue_ok and review_ok and status_ok):
            issue = await client.post(
                f"{base}/repos/{repo}/issues/0/comments", headers=auth, json={"body": "probe"},
            )
            review = await client.post(
                f"{base}/repos/{repo}/pulls/0/reviews", headers=auth, json={"event": "COMMENT"},
            )
            issue_s = _github_probe_status(issue.status_code)
            review_s = _github_probe_status(review.status_code)
            issue_ok = issue_ok or issue_s == "ok"
            review_ok = review_ok or review_s == "ok"
            if issue_s == "missing" or review_s == "missing":
                issue_denied = issue_denied or issue_s == "missing"
                review_denied = review_denied or review_s == "missing"
                if not comment_hint:
                    resp = issue if issue_s == "missing" else review
                    comment_hint = _github_deny_hint(resp)

        if not status_ok:
            st = await client.post(
                f"{base}/repos/{repo}/statuses/0000000000000000000000000000000000000000",
                headers=auth, json={"state": "success", "description": "probe"},
            )
            st_s = _github_probe_status(st.status_code)
            if st_s == "ok":
                status_ok = True
            elif st_s == "missing":
                status_denied = True
                if not status_hint:
                    status_hint = _github_deny_hint(st)

        if issue_ok and review_ok and status_ok:
            break

    if issue_ok and review_ok:
        comment = _cap("post_comment", "ok", f"写探针通过：{repos[0]} 可发总结/行级评论")
    elif (issue_denied and not issue_ok) or (review_denied and not review_ok):
        missing = [
            *(["Issues: write（总结评论）"] if issue_denied and not issue_ok else []),
            *(["Pull requests: write（行级评论）"] if review_denied and not review_ok else []),
        ]
        detail = f"{repos[0]} 写探针被拒：缺 {'、'.join(missing)} 权限"
        if comment_hint:
            detail = f"{detail}（GitHub 提示：{comment_hint}）"
        comment = _cap("post_comment", "missing", detail)
    else:
        comment = _cap("post_comment", "unknown", "写探针响应异常，未能判定评论权限")

    if status_ok:
        status = _cap("commit_status", "ok", f"写探针通过：{repos[0]} 可写 commit status")
    elif status_denied:
        detail = f"{repos[0]} 写探针被拒：缺 Commit statuses: write 权限"
        if status_hint:
            detail = f"{detail}（GitHub 提示：{status_hint}）"
        status = _cap("commit_status", "missing", detail)
    else:
        status = _cap("commit_status", "unknown", "写探针响应异常，未能判定 commit status 权限")
    return comment, status


async def probe_capabilities(
    provider: str, url: str, token: str, *, http: httpx.AsyncClient | None = None
) -> list[Capability]:
    """按系统所需能力逐项探测 token 支持情况，返回能力矩阵（详见模块 docstring）。

    注入的 client 归调用方所有，探测结束不关闭；仅在自建 client 时负责关闭。
    """
    base = str(url).rstrip("/")

    async def _client() -> httpx.AsyncClient:  # 自建则在 with 块结束后关闭
        return http if http is not None else httpx.AsyncClient(timeout=10.0)

    if provider == GITHUB:
        client = await _client()
        auth = {"Authorization": f"Bearer {token}"}
        try:
            resp = await client.get(f"{base}/user", headers=auth)
            if resp.status_code >= 400:
                return _github_scope_caps(set(), conn_ok=False)
            scopes = {
                s.strip().lower() for s in (resp.headers.get("X-OAuth-Scopes") or "").split(",") if s.strip()
            }
            if scopes:
                return _github_scope_caps(scopes, conn_ok=True)
            # 头缺失：能拿到明文 token → 按前缀识别类型；fine-grained 用读/写探针逐项判定
            if _github_token_type(token) == "fine-grained":
                repos = await client.get(
                    f"{base}/user/repos?per_page={_MAX_PROBE_REPOS}&sort=updated", headers=auth,
                )
                return await _github_fine_grained_probe(client, base, auth, repos)
            return _github_no_scope_caps(token)
        finally:
            await client.aclose()

    # —— Gitee（API v5，access_token 查询参数认证）——
    if provider == GITEE:
        client = await _client()
        auth = {"access_token": token}
        try:
            user = await client.get(f"{base}/user", params=auth)
            repos = await client.get(f"{base}/user/repos", params={**auth, "per_page": 1})
        finally:
            await client.aclose()
        if user.status_code >= 400:
            return [
                _cap("connect", "missing", f"认证失败（HTTP {user.status_code}）"),
                *[_cap(n, "unknown", "认证未通过，无法判定") for n in CAPABILITY_ORDER if n != "connect"],
            ]
        out = [_cap("connect", "ok")]
        if repos.status_code < 400:
            out.append(_cap("read_pull", "ok"))
        elif repos.status_code in (401, 403):
            out.append(_cap("read_pull", "missing", f"读仓库列表被拒（HTTP {repos.status_code}），需 projects 权限"))
        else:
            out.append(_cap("read_pull", "unknown", f"读探针 HTTP {repos.status_code}"))
        # 写探针有副作用不实际发；Gitee 不暴露 scope 头 → 诚实标 unknown（与 GitLab 同策略）
        out.append(_cap("post_comment", "unknown", "Gitee 不暴露 scope；发评论需 projects 权限（未做写探针避免副作用）"))
        out.append(_cap("commit_status", "unknown", "同上：写状态需 projects 权限（未做写探针）"))
        return out

    # —— GitLab ——
    client = await _client()
    user = await client.get(f"{base}/api/v4/user", headers={"PRIVATE-TOKEN": token})
    projects = await client.get(
        f"{base}/api/v4/projects?membership=true&per_page=1", headers={"PRIVATE-TOKEN": token},
    )
    await client.aclose()
    if user.status_code >= 400:
        return [
            _cap("connect", "missing", f"认证失败（HTTP {user.status_code}）"),
            *[_cap(n, "unknown", "认证未通过，无法判定") for n in CAPABILITY_ORDER if n != "connect"],
        ]
    out = [_cap("connect", "ok")]
    if projects.status_code < 400:
        out.append(_cap("read_pull", "ok"))
    elif projects.status_code in (401, 403):
        out.append(_cap("read_pull", "missing", f"读项目列表被拒（HTTP {projects.status_code}），需 read_api / api 权限"))
    else:
        out.append(_cap("read_pull", "unknown", f"读探针 HTTP {projects.status_code}"))
    # 写探针有副作用，不实际发；GitLab 不暴露 scope 头 → 诚实标 unknown
    out.append(_cap("post_comment", "unknown", "GitLab 不暴露 scope；发评论需 api 权限（未做写探针避免副作用）"))
    out.append(_cap("commit_status", "unknown", "同上：写状态需 api 权限（未做写探针）"))
    return out
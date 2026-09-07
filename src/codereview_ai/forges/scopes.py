"""平台接入能力探测（DESIGN §9 补拉通道的权限盲区诊断）。

「测试连接」不再只回一个 bool，而是按**系统所需能力**逐项探测当前 token 的支持情况，
返回「可用 / 缺权限 / 未知」矩阵供后台展示，让用户一眼看出「能补拉吗 / 能评论吗」。

判定策略：
- **GitHub**：`GET /user` 成功后在响应头读权威的 `X-OAuth-Scopes`，与各能力所需 scope 求交
  （`repo` / `public_repo` / `repo:status` 等）。头缺失（如 fine-grained PAT 不返回 scope）→ 标
  `unknown`，诚实不猜；`/user` 非 2xx → 认证本身失败，connect=missing、其余 unknown。
- **GitLab**：不再验证 scope（PAT 不从 `/user` 暴露 scope），改为**读探针**——`/user` 判 connect、
  `/projects` 判 read_pull（补拉/拉 files 只要求读权限）；评论/状态是**写**权限，避免做真实写探针
  （有副作用），一律标 `unknown` + 提示需 `api` 权限，诚实降级。

注入 httpx client 便于离线测试（MockTransport），与 forge 适配器同一风格。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from codereview_ai.forges.signatures import GITHUB

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
    "commit_status": "写 commit 状态（F3.7 评分卡，可选）",
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


def _github_scope_caps(scopes: set[str], conn_ok: bool) -> list[Capability]:
    if not conn_ok:
        return [
            _cap("connect", "missing", "认证失败"),
            *[_cap(n, "unknown", "认证未通过，无法判定") for n in CAPABILITY_ORDER if n != "connect"],
        ]
    # /user 通但没返回 scope 头（fine-grained PAT / 受限 token）→ 无法精确判定，标 unknown
    if not scopes:
        return [
            _cap("connect", "ok"),
            *[_cap(n, "unknown", "未返回 X-OAuth-Scopes（可能是 fine-grained PAT / 受限 token）")
              for n in CAPABILITY_ORDER if n != "connect"],
        ]
    out = [_cap("connect", "ok")]
    for name, need in GITHUB_REQUIRED_SCOPES.items():
        have = scopes & set(need)
        if have:
            out.append(_cap(name, "ok"))
        else:
            out.append(_cap(name, "missing", f"缺 scope：{', '.join(need)}"))
    return out


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
        resp = await client.get(f"{base}/user", headers={"Authorization": f"Bearer {token}"})
        await client.aclose()
        if resp.status_code >= 400:
            return _github_scope_caps(set(), conn_ok=False)
        scopes = {
            s.strip().lower() for s in (resp.headers.get("X-OAuth-Scopes") or "").split(",") if s.strip()
        }
        return _github_scope_caps(scopes, conn_ok=True)

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
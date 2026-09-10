"""DB 驱动配置（DESIGN §16）：把后台落库的模型/通知配置合并成 worker 可用的解析结果。

分层（低→高）：内嵌默认 < DB 拉取（每次调用实时查询，不做内存缓存）
< env 重放。env 重放放最后，且**只补缺失、绝不覆盖**——host 侧已显式配置的
`CR_LLM_MODEL` / `CR_*_TOKEN` / `CR_*_API_KEY` 永远优先于 DB，保证密钥压不住。

```python
内置默认 → DB(model_config,priority 最高) → 请求级 context → env 重放(缺失才补) → 运行时参数
```

`REPO_OVERRIDABLE_KEYS_BY_SECTION` 白名单驱动「哪些键允许被项目覆盖」，禁词清单
（`api_key`/`secret`/`webhook_secret`/`token`/`push_outputs`/`prompt_fragments`）一票否决，
后台编辑校验与 webhook 覆盖校验共用同一张白名单，两个入口不会漂移（§16）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.crypto import decrypt
from codereview_ai.review.fallback import wrap_fallback
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import (
    ForgeConfig,
    ModelConfig,
    NotifierConfig,
    NotifierMember,
    NotifierRouteMember,
    Project,
    Role,
    User,
    Workspace,
    WorkspaceForgeConfig,
    WorkspaceModelConfig,
)

logger = logging.getLogger("codereview_ai.config_repository")

#: host-only 禁词清单：命中任一即拒绝作为项目覆盖键（§16）
FORBIDDEN_OVERRIDE_WORDS = (
    "api_key",
    "api_key_encrypted",
    "secret",
    "webhook_secret",
    "token",
    "push_outputs",
    "prompt_fragments",
)

#: 平台默认 URL：env 配了 token 但没配 URL、或 DB 缺省时兜底（GitHub 公共托管 / GitLab 自托管示例）
DEFAULT_FORGE_URLS: dict[str, str] = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
}

#: 项目可覆盖的分节 → 允许键白名单（DESIGN §16：同一张表驱动编辑与覆盖两处校验）。
#: host-only 配置（密钥/URL/LLM 路由/通知签名）一律不在其列。
REPO_OVERRIDABLE_KEYS_BY_SECTION: dict[str, set[str]] = {
    "review": {"style"},
    "filter": {"extensions", "exclude_paths", "max_lines", "max_bytes"},
}


# ---------------------------------------------------------------------------
# 覆盖白名单校验
# ---------------------------------------------------------------------------

def _section_of(key: str) -> str:
    return key.split(".", 1)[0]


def _leaf_of(key: str) -> str:
    return key.split(".", 1)[1]


def is_overrideable(key: str) -> bool:
    """键是否允许被项目覆盖：必须在白名单分节内，且不含任一禁词。"""
    if any(word in key.lower() for word in FORBIDDEN_OVERRIDE_WORDS):
        return False
    allowed = REPO_OVERRIDABLE_KEYS_BY_SECTION.get(_section_of(key))
    if allowed is None:
        return False
    return _leaf_of(key) in allowed


def validate_override_key(key: str, *, value: Any = None) -> None:
    """校验一个项目覆盖键是否合法；不合法抛 ValueError（供后台表单/webhook 复用）。

    `value` 仅为随日志记录覆盖的**分节名**（不记值，值可能含密钥，见 §15.2 脱敏）。
    """
    if any(word in key.lower() for word in FORBIDDEN_OVERRIDE_WORDS):
        raise ValueError(f"键 {key!r} 命中禁词清单，禁止作为项目覆盖项（host-only）")
    if not is_overrideable(key):
        sections = sorted(REPO_OVERRIDABLE_KEYS_BY_SECTION)
        raise ValueError(f"键 {key!r} 不在可覆盖白名单内（{sections}）")  # noqa: E501


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------

@dataclass
class ResolvedLLM:
    """一个可用的 LLM 解析结果（来自 DB model_config，优先级最高者）。"""

    name: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    env: dict[str, str] = field(default_factory=dict)  # 供 env 重放的 {provider}_api_key / api_base

    def __post_init__(self) -> None:
        if self.provider and self.api_key and f"{self.provider}_api_key" not in self.env:
            self.env[f"{self.provider}_api_key"] = self.api_key
        if self.base_url and "api_base" not in self.env:
            self.env["api_base"] = self.base_url


@dataclass
class NotifierRoute:
    """一条通知路由（channel + 解密后的 webhook/secret，及 @ 阈值 + 勾选成员）。

    `at_members` 是该渠道勾选的系统级成员（去重后）；推送时由 dispatch 按
    `channel` 取对应平台 ID，与「提交者主动解析」合并后再过 @ 阈值门控。
    """

    channel: str
    webhook: str
    secret: str
    project_id: int | None
    at_threshold: int
    at_all: bool = False  # 评分低于阈值时是否 @所有人
    at_members: list[NotifierMember] = field(default_factory=list)


@dataclass
class ResolvedForge:
    """一个平台接入解析结果（url + 解密后 token）；source 标 env 或 DB。"""

    provider: str
    url: str
    token: str
    source: str = "db"


class ConfigRepository:
    """把 DB 模型/通知配置解析成 worker 可直接使用的设施，带 TTL 缓存与 env 重放。"""

    def __init__(self, engine: AsyncEngine, *, encryption_key: str) -> None:
        self._engine = engine
        self._enc = encryption_key if isinstance(encryption_key, str) else ""
        self._models: list[ModelConfig] = []
        self._notifiers: list[NotifierConfig] = []
        self._forges: list[ForgeConfig] = []
        self._members: dict[int, NotifierMember] = {}  # 系统级成员名单
        self._route_members: dict[int, list[int]] = {}  # notifier_id → member_ids

    # —— 拉取 ——

    async def _fetch(self) -> None:
        """每次调用实时从 DB 拉取启用的模型/通知/平台（内存 TTL 缓存已移除）。"""
        session = session_factory(self._engine)
        async with session() as s:
            models = (await s.execute(
                select(ModelConfig)
                .where(ModelConfig.enabled.is_(True))
                .order_by(ModelConfig.priority.desc(), ModelConfig.id)
            )).scalars().all()
            notifiers = (await s.execute(
                select(NotifierConfig).where(NotifierConfig.enabled.is_(True))
            )).scalars().all()
            forges = (await s.execute(
                select(ForgeConfig)
                .where(ForgeConfig.enabled.is_(True))
                .order_by(ForgeConfig.provider)
            )).scalars().all()
            members = (await s.execute(select(NotifierMember))).scalars().all()
            links = (await s.execute(select(NotifierRouteMember))).scalars().all()
        self._models = list(models)
        self._notifiers = list(notifiers)
        self._forges = list(forges)
        self._members = {m.id: m for m in members}
        self._route_members = {}
        for link in links:
            self._route_members.setdefault(link.notifier_id, []).append(link.member_id)

    # —— 解析 ——

    async def resolve_llm(self) -> ResolvedLLM | None:
        """返回可用 LLM 配置；env 已显式配 `CR_LLM_MODEL` 时以 env 为准（重放压 DB）。"""
        env_model = (os.environ.get("CR_LLM_MODEL") or "").strip()
        if env_model:
            return ResolvedLLM(name=env_model, provider="", model=env_model)
        await self._fetch()
        if not self._models:
            return None
        top = self._models[0]  # 已按 priority 降序
        api_key = decrypt(top.api_key_encrypted, self._enc) if top.api_key_encrypted else ""
        return ResolvedLLM(
            name=top.name,
            provider=top.provider,
            model=top.model or top.name,
            api_key=api_key,
            base_url=top.base_url,
            temperature=top.temperature,
            max_tokens=top.max_tokens,
        )

    async def resolve_llm_chain(self) -> list[ResolvedLLM]:
        """返回全部启用模型组成的审查回退链（priority 降序，`_fetch` 已排）。

        env 已显式配 `CR_LLM_MODEL` 时固定返回该模型（DB 让位）。链上每台模型解密
        key 供 `wrap_fallback` 构造独立 `LLMGateway`——主模型失败自动切下一台。
        """
        env_model = (os.environ.get("CR_LLM_MODEL") or "").strip()
        if env_model:
            return [ResolvedLLM(name=env_model, provider="", model=env_model)]
        await self._fetch()
        if not self._models:
            return []
        chain: list[ResolvedLLM] = []
        for m in self._models:  # _fetch 已按 priority desc, id 排序
            api_key = decrypt(m.api_key_encrypted, self._enc) if m.api_key_encrypted else ""
            chain.append(ResolvedLLM(
                name=m.name,
                provider=m.provider,
                model=m.model or m.name,
                api_key=api_key,
                base_url=m.base_url,
                temperature=m.temperature,
                max_tokens=m.max_tokens,
            ))
        return chain

    async def _project_is_tenant_owned(self, project_id: int) -> bool:
        """项目是否「租户所有」：其归属 workspace 的 owner 是**非超管**用户。

        多租户收口（阶段 B）：自助注册用户（`member`，私有 workspace owner=本人）建的项目
        属租户所有；超管/管理端空间（默认工作区 owner=admin is_super）的项目是运营者自己的。
        无归属 workspace / owner 为空 → False（服务端管理空间，走全局默认）。
        """
        session = session_factory(self._engine)
        async with session() as s:
            ws_id = (await s.execute(
                select(Project.workspace_id).where(Project.id == project_id)
            )).scalar_one_or_none()
            if ws_id is None:
                return False
            owner_id = (await s.execute(
                select(Workspace.owner_id).where(Workspace.id == ws_id)
            )).scalar_one_or_none()
            if owner_id is None:
                return False
            is_super = (await s.execute(
                select(Role.is_super)
                .join(User, User.role_id == Role.id)
                .where(User.id == owner_id)
            )).scalar_one_or_none()
        return bool(is_super) is False

    async def notifier_routes(self, project_id: int | None = None) -> list[NotifierRoute]:
        """给出项目的通知路由（无项目号时含全局默认）；隐式密钥解密，日志只记 channel。

        **多租户收口（阶段 B）**：项目归属**租户所有**（workspace owner 非超管）时，跳过
        `project_id IS NULL` 的全局默认渠道——租户审查内容不再被推到运营者的全局/共享渠道，
        只发该项目显式配置的路由（保守默认：未配置即静默，不外泄）。运营者自己的项目照旧
        走全局默认 + 项目级覆盖。
        """
        await self._fetch()
        tenant_owned = (
            project_id is not None and await self._project_is_tenant_owned(project_id)
        )
        routes: list[NotifierRoute] = []
        for n in self._notifiers:
            if n.project_id is not None and n.project_id != project_id:
                continue  # 项目级路由不匹配 → 跳过；NULL 全局默认始终适用
            if tenant_owned and n.project_id is None:
                continue  # 保守收口：租户项目不发全局默认渠道
            routes.append(NotifierRoute(
                channel=n.channel,
                webhook=decrypt(n.webhook_encrypted, self._enc) if n.webhook_encrypted else "",
                secret=decrypt(n.secret_encrypted, self._enc) if n.secret_encrypted else "",
                project_id=n.project_id,
                at_threshold=n.at_threshold,
                at_all=n.at_all,
                at_members=[self._members[mid] for mid in self._route_members.get(n.id, [])
                            if mid in self._members],
            ))
        return routes

    async def resolve_member_by_git_username(
        self, git_username: str
    ) -> NotifierMember | None:
        """按 forge 提交用户名命中系统级成员（`pr.author` 的解析键）。

        供 dispatch 把「提交者 @」从裸 username 转成该平台认识的 ID；命中不到返回
        None——此时作者只进文案点名、不在该渠道 @。每次调用随 `_fetch` 实时刷新。
        """
        if not git_username:
            return None
        await self._fetch()
        for m in self._members.values():
            if m.git_username == git_username:
                return m
        return None

    async def resolve_forge(self, provider: str) -> ResolvedForge | None:
        """返回一个平台的接入凭据；**env 优先、DB 兜底**（host env 压不住）。

        与 `resolve_llm` 语义一致：env 显式配了 `CR_{PROVIDER}_TOKEN` → 用 env；
        否则读 DB `forge_config` 该 provider 的启用行（token 解密）。无 token
        返回 None（该平台不注册）。每次调用实时读取 DB，后台保存后立即生效。
        """
        key = provider.upper()
        env_token = (os.environ.get(f"CR_{key}_TOKEN") or "").strip()
        if env_token:
            env_url = (os.environ.get(f"CR_{key}_URL") or "").strip()
            return ResolvedForge(
                provider,
                env_url or DEFAULT_FORGE_URLS.get(provider, ""),
                env_token,
                source="env",
            )
        await self._fetch()
        for f in self._forges:
            if f.provider != provider:
                continue
            token = decrypt(f.token_encrypted, self._enc) if f.token_encrypted else ""
            return ResolvedForge(provider, f.url or DEFAULT_FORGE_URLS.get(provider, ""), token)
        return None

    # —— BYOK：workspace 自带凭据（必须自带 key；缺失除非豁免否则降级）——

    async def _workspace_fallback_enabled(self, workspace_id: int) -> bool:
        """该 workspace 是否被超管显式豁免（`platform_fallback`），缺失空间视为 False。"""
        session = session_factory(self._engine)
        async with session() as s:
            val = (await s.execute(
                select(Workspace.platform_fallback).where(Workspace.id == workspace_id)
            )).scalar_one_or_none()
        return bool(val)

    async def _project_workspace_id(self, project_id: int) -> int | None:
        """返回项目归属的 workspace_id；空/无归属 → None。"""
        session = session_factory(self._engine)
        async with session() as s:
            return (await s.execute(
                select(Project.workspace_id).where(Project.id == project_id)
            )).scalar_one_or_none()

    async def resolve_workspace_llm_chain(self, workspace_id: int) -> list[ResolvedLLM]:
        """返回某 workspace 自带的启用 LLM 链（BYOK，priority 降序）。

        空 = 该空间未自带任何可用模型（由调用方决定降级 or 豁免回落全局）。不 consult env。
        """
        session = session_factory(self._engine)
        rows: list[WorkspaceModelConfig] = []
        async with session() as s:
            rows = list((await s.execute(
                select(WorkspaceModelConfig)
                .where(
                    WorkspaceModelConfig.workspace_id == workspace_id,
                    WorkspaceModelConfig.enabled.is_(True),
                )
                .order_by(WorkspaceModelConfig.priority.desc(), WorkspaceModelConfig.id)
            )).scalars().all())
        chain: list[ResolvedLLM] = []
        for m in rows:
            api_key = decrypt(m.api_key_encrypted, self._enc) if m.api_key_encrypted else ""
            chain.append(ResolvedLLM(
                name=m.name,
                provider=m.provider,
                model=m.model or m.name,
                api_key=api_key,
                base_url=m.base_url,
                temperature=m.temperature,
                max_tokens=m.max_tokens,
            ))
        return chain

    async def resolve_workspace_forge(
        self, provider: str, workspace_id: int
    ) -> ResolvedForge | None:
        """返回某 workspace 自带的该 provider 凭据（BYOK）。缺权威配置 → None
        （不回落 env/全局）。"""
        session = session_factory(self._engine)
        async with session() as s:
            row = (await s.execute(
                select(WorkspaceForgeConfig)
                .where(
                    WorkspaceForgeConfig.workspace_id == workspace_id,
                    WorkspaceForgeConfig.provider == provider,
                    WorkspaceForgeConfig.enabled.is_(True),
                )
            )).scalars().first()
        if row is None:
            return None
        token = decrypt(row.token_encrypted, self._enc) if row.token_encrypted else ""
        return ResolvedForge(provider, row.url or DEFAULT_FORGE_URLS.get(provider, ""), token)

    async def build_workspace_reviewer(
        self, workspace_id: int, backend: Any = None
    ) -> Reviewer | None:
        """构造某 workspace 的 Reviewer（BYOK：自带链；无自带且豁免 → 回落全局；否则 None 降级）。
        """
        chain = await self.resolve_workspace_llm_chain(workspace_id)
        if not chain:
            if not await self._workspace_fallback_enabled(workspace_id):
                return None
            chain = await self.resolve_llm_chain()
        if not chain:
            return None
        self.apply_env_replay(chain[0])
        return Reviewer(wrap_fallback(chain, backend=backend))

    # —— BYOK：给定项目/仓库 → 本轮审查应使用的 reviewer / forge（决策统一入口）——

    async def _project_id(self, provider: str, repo_id: str) -> int | None:
        """按启用项目 (provider, repo_id) 定位 project.id；未注册 → None。"""
        session = session_factory(self._engine)
        async with session() as s:
            return (await s.execute(
                select(Project.id).where(
                    Project.provider == provider,
                    Project.repo_id == repo_id,
                    Project.enabled.is_(True),
                )
            )).scalars().first()

    async def resolve_reviewer_for_project(self, project_id: int) -> Reviewer | None:
        """按项目挑 Reviewer：非租户 → 全局；租户 → 自带/豁免回落/降级（None）。"""
        if not await self._project_is_tenant_owned(project_id):
            return await self.build_reviewer()
        ws_id = await self._project_workspace_id(project_id)
        if ws_id is None:
            return None
        return await self.build_workspace_reviewer(ws_id)

    async def resolve_forge_for_project(
        self, provider: str, project_id: int
    ) -> ResolvedForge | None:
        """按项目挑 forge 凭据：非租户 → 全局；租户 → 自带/豁免回落/无（None 跳过）。
        """
        if not await self._project_is_tenant_owned(project_id):
            return await self.resolve_forge(provider)
        ws_id = await self._project_workspace_id(project_id)
        if ws_id is None:
            return None
        forge = await self.resolve_workspace_forge(provider, ws_id)
        if forge is None and await self._workspace_fallback_enabled(ws_id):
            forge = await self.resolve_forge(provider)
        return forge

    async def resolve_reviewer_for_repo(
        self, provider: str, repo_id: str
    ) -> Reviewer | None:
        """worker 工厂：按 (provider, repo_id) 挑 Reviewer；未注册项目 → None（降级）。"""
        pid = await self._project_id(provider, repo_id)
        if pid is None:
            return None
        return await self.resolve_reviewer_for_project(pid)

    async def resolve_forge_for_repo(
        self, provider: str, repo_id: str
    ) -> ResolvedForge | None:
        """worker 工厂：按 (provider, repo_id) 挑 forge 凭据；未注册项目 → None。"""
        pid = await self._project_id(provider, repo_id)
        if pid is None:
            return None
        return await self.resolve_forge_for_project(provider, pid)

    # —— env 重放（只补缺失，绝不覆盖 host env）——

    def apply_env_replay(self, llm: ResolvedLLM | None) -> list[str] | None:
        """把 DB 解析出的 api_key/api_base 补进 env，返回本次实际生效的键列表。

        「env 重放」本意是 host 胜出：用 `setdefault` 只补缺失——host 已显式配置的
        同名环境变量永远不被 DB 覆盖（密钥压不住）。
        """
        if llm is None:
            return None
        applied: list[str] = []
        for key, value in llm.env.items():
            if key not in os.environ:  # env 无此键 → 由 DB 值补入
                os.environ[key] = value
                applied.append(key)
        return applied

    # —— worker 装配 ——

    async def build_reviewer(self, backend: Any = None) -> Reviewer | None:
        """用解析出的 LLM 回退链构造一个 Reviewer；无可用模型返回 None（worker 跳过 LLM 审查）。

        单模型直接返回其网关，多模型包一层 `FallbackLLMGateway`——主模型失败自动切下一台
        （`resolve_llm_chain` 已按 priority 降序）。`backend` 可注入 fake 便于离线测试。
        """
        chain = await self.resolve_llm_chain()
        if not chain:
            return None
        # 保留 host-env 优先（back-compat；其余链节点显式传 key/url）
        self.apply_env_replay(chain[0])
        gateway = wrap_fallback(chain, backend=backend)
        return Reviewer(gateway)

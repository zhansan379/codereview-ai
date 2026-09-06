"""DB 驱动配置（DESIGN §16）：把后台落库的模型/通知配置合并成 worker 可用的解析结果。

分层（低→高）：内嵌默认 < DB 拉取（**15 分钟 TTL 内存缓存**，transient 失败不缓存）
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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from codereview_ai.crypto import decrypt
from codereview_ai.review.llm_gateway import LLMGateway
from codereview_ai.review.reviewer import Reviewer
from codereview_ai.storage.db import session_factory
from codereview_ai.storage.models import ForgeConfig, ModelConfig, NotifierConfig

logger = logging.getLogger("codereview_ai.config_repository")

#: 内存缓存 TTL（DESIGN §16：15 分钟）
CACHE_TTL_SECONDS = 15 * 60

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
    env: dict[str, str] = field(default_factory=dict)  # 供 env 重放的 {provider}_api_key / api_base

    def __post_init__(self) -> None:
        if self.provider and self.api_key and f"{self.provider}_api_key" not in self.env:
            self.env[f"{self.provider}_api_key"] = self.api_key
        if self.base_url and "api_base" not in self.env:
            self.env["api_base"] = self.base_url


@dataclass
class NotifierRoute:
    """一条通知路由（channel + 解密后的 webhook/secret，及 @ 阈值）。"""

    channel: str
    webhook: str
    secret: str
    project_id: int | None
    at_threshold: int


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
        self._loaded_at: datetime | None = None

    # —— 缓存 & 拉取 ——

    def _fresh(self) -> bool:
        if self._loaded_at is None:
            return False
        return datetime.now(UTC) - self._loaded_at < timedelta(seconds=CACHE_TTL_SECONDS)

    async def _fetch(self) -> None:
        """从 DB 拉取启用的模型/通知/平台；成功才更新缓存与时间戳（transient 失败不缓存）。"""
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
        self._models = list(models)
        self._notifiers = list(notifiers)
        self._forges = list(forges)
        self._loaded_at = datetime.now(UTC)

    async def _ensure_loaded(self, *, force: bool = False) -> None:
        if not force and self._fresh():
            return
        try:
            await self._fetch()
        except Exception as exc:  # noqa: BLE001  DB 暂不可用（transient）
            if self._models or self._notifiers:
                logger.warning("DB 配置刷新失败，沿用上次缓存（ttl 未刷新）: %s", exc)
                return  # 保留旧缓存，不更新时间戳 → 下次调用会再试
            logger.warning("DB 配置拉取失败且无缓存，按空配置降级: %s", exc)
            self._models = []
            self._notifiers = []
            self._forges = []
            self._loaded_at = None  # 失败不置缓存时间戳

    # —— 解析 ——

    async def resolve_llm(self) -> ResolvedLLM | None:
        """返回可用 LLM 配置；env 已显式配 `CR_LLM_MODEL` 时以 env 为准（重放压 DB）。"""
        env_model = (os.environ.get("CR_LLM_MODEL") or "").strip()
        if env_model:
            return ResolvedLLM(name=env_model, provider="", model=env_model)
        await self._ensure_loaded()
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
        )

    async def notifier_routes(self, project_id: int | None = None) -> list[NotifierRoute]:
        """给出项目的通知路由（无项目号时含全局默认）；隐式密钥解密，日志只记 channel。"""
        await self._ensure_loaded()
        routes: list[NotifierRoute] = []
        for n in self._notifiers:
            if n.project_id is not None and n.project_id != project_id:
                continue  # 项目级路由不匹配 → 跳过；NULL 全局默认始终适用
            routes.append(NotifierRoute(
                channel=n.channel,
                webhook=decrypt(n.webhook_encrypted, self._enc) if n.webhook_encrypted else "",
                secret=decrypt(n.secret_encrypted, self._enc) if n.secret_encrypted else "",
                project_id=n.project_id,
                at_threshold=n.at_threshold,
            ))
        return routes

    async def resolve_forge(
        self, provider: str, *, force: bool = False
    ) -> ResolvedForge | None:
        """返回一个平台的接入凭据；**env 优先、DB 兜底**（host env 压不住）。

        与 `resolve_llm` 语义一致：env 显式配了 `CR_{PROVIDER}_TOKEN` → 用 env；
        否则读 DB `forge_config` 该 provider 的启用行（token 解密）。`force=True`
        清 TTL 强制重拉，供后台保存后热更。都无 token 返回 None（该平台不注册）。
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
        await self._ensure_loaded(force=force)
        for f in self._forges:
            if f.provider != provider:
                continue
            token = decrypt(f.token_encrypted, self._enc) if f.token_encrypted else ""
            return ResolvedForge(provider, f.url or DEFAULT_FORGE_URLS.get(provider, ""), token)
        return None

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
        """用解析出的 LLM 构造一个 Reviewer；无可用模型返回 None（worker 跳过 LLM 审查）。

        env 已显式配置 `CR_LLM_MODEL` 时按其模型名构造（DB 模型让位）。api_key/api_base
        通过 env 重放补进进程环境，litellm 在调用期读取。`backend` 可注入 fake 便于离线测试。
        """
        llm = await self.resolve_llm()
        if llm is None:
            return None
        self.apply_env_replay(llm)
        gateway = LLMGateway(model=llm.model or llm.name, backend=backend)
        return Reviewer(gateway)

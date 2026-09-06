"""Webhook 来源识别 + 签名校验（DESIGN §9 / reference platform_payload_map §1-2）。

- 来源识别靠 HTTP header：Gitea 请求**同时**带 `X-Gitea-Event` 和 `X-GitHub-Event`
  （Gitea 刻意兼容 GitHub），所以必须先判 Gitea，否则会被错认成 GitHub。GitLab
  无事件 header，只能由 body 里的 `object_kind` 判定，故作为兜底。
- 签名校验必须对**原始 request body bytes** 做，不能对 `json.dumps(parsed)` 做
  （重序列化会改空格/键序，签名必炸）。所有比对一律 `hmac.compare_digest`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

GITLAB = "gitlab"
GITHUB = "github"
GITEA = "gitea"
GITEE = "gitee"


def detect_forge(headers: Mapping[str, str]) -> str:
    """按 header 识别来源平台；GitLab 无事件 header，作为默认兜底。

    顺序固定：Gitea 先于 GitHub（Gitea 请求同时带 X-Gitea-Event 和 X-GitHub-Event）。
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    for name, provider in (
        ("x-gitea-event", GITEA),
        ("x-github-event", GITHUB),
        ("x-gitee-event", GITEE),
    ):
        if name in lowered:
            return provider
    return GITLAB


def _sha256_hmac(secret: str, data: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()


def verify_signature(
    provider: str,
    secret: str,
    headers: Mapping[str, str],
    raw_body: bytes,
) -> bool:
    """校验某平台请求的签名。secret 为空时视为未配置 → 拒绝（安全默认）。"""
    lowered = {k.lower(): v.strip() for k, v in headers.items()}
    if provider == GITHUB:
        expected = lowered.get("x-hub-signature-256")
        if not expected:
            return False
        actual = "sha256=" + _sha256_hmac(secret, raw_body).hex()
        return hmac.compare_digest(actual, expected)
    if provider == GITEA:
        expected = lowered.get("x-gitea-signature")
        if not expected:
            return False
        actual = _sha256_hmac(secret, raw_body).hex()
        return hmac.compare_digest(actual, expected)
    if provider == GITEE:
        token = lowered.get("x-gitee-token")
        ts = lowered.get("x-gitee-timestamp")
        if not token or not ts:
            return False
        mac = hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode(), hashlib.sha256)
        actual = base64.b64encode(mac.digest()).decode("utf-8")
        return hmac.compare_digest(actual, token)
    # GitLab：明文 token 直接比对（GitLab 不提供 HMAC）
    token = lowered.get("x-gitlab-token")
    if not token:
        return False
    return hmac.compare_digest(token, secret)


def body_object_kind(data: dict[str, Any]) -> str:
    """GitLab 兜底：由 body 的 object_kind 判断事件类型。"""
    return str(data.get("object_kind") or "")

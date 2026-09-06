"""forges/signatures 测试：来源识别 + 各平台签名校验（含时序安全比对）。"""

from __future__ import annotations

import base64
import hashlib
import hmac

from codereview_ai.forges.signatures import detect_forge, verify_signature

SECRET = "s3cret"


def _gh(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_detect_forge_prefers_gitea_over_github():
    # Gitea 请求同时带 Gitea 与 GitHub header → 必须判为 gitea
    headers = {"X-Gitea-Event": "pull_request", "X-GitHub-Event": "pull_request"}
    assert detect_forge(headers) == "gitea"


def test_detect_forge_github_and_gitlab_default():
    assert detect_forge({"X-GitHub-Event": "pull_request"}) == "github"
    # GitLab 无事件 header → 兜底 gitlab
    assert detect_forge({"Content-Type": "application/json"}) == "gitlab"


def test_verify_github_signature():
    body = b'{"a": 1}'
    assert verify_signature("github", SECRET, {"X-Hub-Signature-256": _gh(body)}, body) is True
    assert verify_signature("github", SECRET, {"X-Hub-Signature-256": _gh(b"tampered")}, body) is False


def test_verify_gitlab_plain_token():
    assert verify_signature("gitlab", SECRET, {"X-Gitlab-Token": SECRET}, b"{}") is True
    assert verify_signature("gitlab", SECRET, {"X-Gitlab-Token": "wrong"}, b"{}") is False
    assert verify_signature("gitlab", SECRET, {}, b"{}") is False


def test_verify_gitea_signature():
    body = b'{"a": 1}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature("gitea", SECRET, {"X-Gitea-Signature": sig}, body) is True
    assert verify_signature("gitea", SECRET, {"X-Gitea-Signature": "bad"}, body) is False


def test_verify_gitee_signature():
    body = b"{}"
    ts = "1725000000"
    mac = hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256)
    token = base64.b64encode(mac.digest()).decode()
    headers = {"X-Gitee-Token": token, "X-Gitee-Timestamp": ts}
    assert verify_signature("gitee", SECRET, headers, body) is True
    assert verify_signature("gitee", SECRET, {**headers, "X-Gitee-Timestamp": "0"}, body) is False


def test_signature_is_not_recomputed_from_reserialized_body():
    # 关键：必须对原始 bytes 做 HMAC；重序列化会改键序导致签名必炸
    body = b'{"z": 1, "a": 2}'
    sig = _gh(body)
    # 对重排键后的字符串验签应当失败
    assert verify_signature("github", SECRET, {"X-Hub-Signature-256": sig}, b'{"a": 2, "z": 1}') is False

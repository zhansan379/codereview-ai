"""阶段 D 开放安全加固：登录限速 + 算术验证码（自托管，零新依赖）。

**登录限速**：内存滑动窗口，键 = 客户端 IP 与 `ip:username` 双维度；超窗口阈值即 429。
多进程/重启即清零——对单进程 uvicorn 部署足够，且重启自动重置限制是合理行为（攻击者
无法靠重启缓解窗口，运维重启反而是自然的冷却）。多 worker 横向扩容需要换成共享存储
（Redis/DB），本期不做（注释留档）。

**验证码**：简单算术题 `a + b = ?`（a,b∈[2,9]）。不引 PIL/图片 CDN 依赖、登录框一个
文本框即完成、读屏可无障碍朗读；配合限速足以挡脚本化爆破（不追求挡高成本人工打码）。
答案只存**sha256**哈希、一次性、TTL 过期即失效。

`LoginGuard` 挂到 `app.state.login_guard`，测试可用 `reset()` 隔离。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator

from codereview_ai.security import generate_token, hash_token


class LoginGuard:
    """IP 限速 + 用户名维度失败计数 + 算术验证码签发/校验。"""

    def __init__(
        self,
        *,
        login_rate_attempts: int = 10,
        login_rate_window_seconds: int = 900,
        captcha_threshold_attempts: int = 3,
        captcha_ttl_seconds: int = 300,
    ) -> None:
        self._attempts = login_rate_attempts
        self._window = login_rate_window_seconds
        self._captcha_threshold = captcha_threshold_attempts
        self._captcha_ttl = captcha_ttl_seconds

        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)   # ip / ip:username
        self._failures: dict[str, int] = defaultdict(int)          # ip:username 失败计数
        self._captchas: dict[str, tuple[str, float]] = {}          # id -> (answer_hash, expires_at)

    # ---------------- 限速 ----------------

    def _prune(self, key: str) -> deque[float]:
        dq = self._hits[key]
        cutoff = time.monotonic() - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    def check_rate(self, ip: str) -> bool:
        """该 IP 是否仍在限速窗口内被允许（True=允许）。窗口内计数 < 阈值。"""
        with self._lock:
            dq = self._prune(ip)
            return len(dq) < self._attempts

    def record_attempt(self, ip: str, username: str) -> None:
        """记一次尝试（无论成败），供速率窗口计数。"""
        with self._lock:
            cutoff = time.monotonic() - self._window
            for key in (ip, f"{ip}:{username}"):
                dq = self._hits[key]
                while dq and dq[0] < cutoff:
                    dq.popleft()
                dq.append(time.monotonic())

    def record_failure(self, ip: str, username: str) -> None:
        with self._lock:
            self._failures[f"{ip}:{username}"] += 1

    def record_success(self, ip: str, username: str) -> None:
        """登录成功：清零该用户（ip:username）失败计数，保留 IP 速率窗口。"""
        with self._lock:
            self._failures.pop(f"{ip}:{username}", None)

    def need_captcha(self, ip: str, username: str) -> bool:
        """当前是否需要验证码：阈值<=0 恒开；阈值<0 关闭；否则失败计数>=阈值。"""
        if self._captcha_threshold < 0:
            return False
        if self._captcha_threshold == 0:
            return True
        with self._lock:
            return self._failures[f"{ip}:{username}"] >= self._captcha_threshold

    # ---------------- 验证码 ----------------

    def new_captcha(self) -> tuple[str, str]:
        """签发一道算术题，返回 (captcha_id, prompt)。只存答案 sha256。"""
        a = secrets.randbelow(8) + 2   # 2..9
        b = secrets.randbelow(8) + 2   # 2..9
        captcha_id = generate_token(16)
        with self._lock:
            self._captchas[captcha_id] = (
                hash_token(str(a + b)),
                time.monotonic() + self._captcha_ttl,
            )
        return captcha_id, f"{a} + {b} = ?"

    def verify_captcha(self, captcha_id: str, answer: str) -> bool:
        """校验一次并**立即作废**（一次性）。过期/不存在 → False。"""
        with self._lock:
            entry = self._captchas.pop(captcha_id, None)
        if entry is None:
            return False
        expected_hash, expires_at = entry
        if time.monotonic() > expires_at:
            return False
        return hash_token(str(answer).strip()) == expected_hash

    # ---------------- 测试/运维 ----------------

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._failures.clear()
            self._captchas.clear()

    def clear_captchas(self) -> None:
        with self._lock:
            self._captchas.clear()

    def _iter_active_captchas(self) -> Iterator[tuple[str, str, float]]:
        # 供可能的运维/清点；本期不挂端点
        with self._lock:
            items = list(self._captchas.items())
        for cid, (_h, exp) in items:
            yield cid, _h, exp
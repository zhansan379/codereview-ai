"""仓库同步：把被审仓库 clone 到本地做 agent 全仓上下文（DESIGN §12.1 补齐）。

借鉴 AI-Codereview-Gitlab `biz/agent/repo_syncer.py` 的机制，但**只取其 git 同步逻辑**：
- 懒 clone（首次）+ 增量 `fetch --all --prune` + `reset --hard <ref>` 到指定 commit/分支；
- 带 `oauth2:<token>` 鉴权注入（http(s) URL），host 匹配平台 token；
- 可移植文件锁（POSIX `fcntl` / Windows `msvcrt`）防并发 clone。

**安全红线（A2 反模式）**：这里只做 git 只读同步，不含任何 shell 执行工具。agent 只能
通过只读结构化工具看这个工作区，无法执行代码，故本同步本身即可靠路径穿越 + 只读保证。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from codereview_ai.domain.models import PullRequest
from codereview_ai.forges.base import repo_path_from_url

logger = logging.getLogger("codereview_ai.agentic.syncer")

#: 项目 key → 安全目录名的非法字符白名单（保留字母数字点横下划线）。
_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def slugify_key(key: str) -> str:
    """把 repo key（如 owner/name）转成合法目录名。"""
    return _KEY_RE.sub("_", key or "repo")


def git_clone_url(pr: PullRequest) -> str:
    """从 `web_url` 派生无凭据的 http(s) clone 地址：``<scheme>://<host>/<path>.git``。

    用 `repo_path_from_url` 解析出 owner/repo（GitHub 取两段、GitLab 截到动作段），
    再拼回原 scheme+host。解析不出返回 ``""``（调用方据此降级）。
    """
    if not pr.web_url:
        return ""
    parsed = urlparse(pr.web_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    path = repo_path_from_url(pr.web_url, pr.provider)
    if not path:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/{path}.git"


def _auth_url(url: str, token: str) -> str:
    """把 http(s) 的 clone URL 注入 ``oauth2:<token>`` 凭据（已带凭据/非 http 则原样）。"""
    if not url or not token:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url
    if "@" in (parsed.netloc or ""):
        return url
    userinfo = f"oauth2:{quote(token, safe='')}"
    return urlunparse(parsed._replace(netloc=f"{userinfo}@{parsed.netloc}"))


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300) -> None:
    """同步跑 git 子命令；失败抛 RuntimeError（含 stderr）。"""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(cmd[:2])} 超时（>{timeout}s）") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("git 可执行文件缺失") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = stderr or f"exit {proc.returncode}"
        raise RuntimeError(f"git {' '.join(cmd[:2])} 失败：{detail}")


class _FileLock:
    """跨平台文件锁：优先 `fcntl`，Windows 退 `msvcrt`，都不行则轻量降级（尽力而为）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def __enter__(self) -> _FileLock:
        self._path.touch(exist_ok=True)
        # Windows `msvcrt.locking` 在 0 字节文件上锁 1 字节（越过 EOF）会抛 OSError，
        # 被下方 `except OSError: pass` 吞掉 → 锁形同虚设，并发会竞争同一 clone 目录。
        # 先把锁文件垫到 ≥1 字节，保证 msvcrt 锁真实生效。
        if self._path.stat().st_size == 0:
            self._path.write_bytes(b"\x00")
        self._fh = open(self._path, "a+")  # noqa: SIM115 —— 跨平台锁句柄，保持打开
        try:
            try:
                import fcntl  # POSIX

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except ImportError:
                try:
                    import msvcrt  # Windows

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                except ImportError:
                    pass  # 无锁原语：尽力而为，不阻塞
        except OSError:
            pass  # 拿不到锁也不阻塞 clone（并发只会多做一次 clone/fetch，幂等安全）
        return self

    def __exit__(self, *_exc) -> None:  # noqa: ANN003
        try:
            try:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                try:
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except ImportError:
                    pass
        except OSError:
            pass
        self._fh.close()


class RepoCloner:
    """把远程仓库惰性 clone 到 `cache_root`，之后增量同步到目标 ref。

    每个 repo 一个子目录：`cache_root/<safe_key>/`（git 工作树）+ 一个 .lock 文件。
    cache 跨审查复用：下次直接 `fetch --all --prune` + `reset --hard` 到位，不重复 clone。
    """

    def __init__(self, cache_root: Path | str, *, timeout: int = 300) -> None:
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    def sync_to(self, *, url: str, key: str, ref: str, token: str = "") -> Path:
        """确保 `key` 仓库本地可用且 `ref` 已 checkout，返回工作树路径。

        `ref` 若是 7-40 位十六进制则按 commit checkout（`reset --hard <sha>`），
        否则按分支（`reset --hard origin/<ref>`，自动跟随远端新提交）。
        """
        if not url:
            raise RuntimeError("缺少 clone URL")
        target = self.cache_root / slugify_key(key)
        lock_path = self.cache_root / f"{slugify_key(key)}.lock"
        auth_url = _auth_url(url, token)
        with _FileLock(lock_path):
            if self._valid_repo(target):
                self._reset_remote(target, auth_url)
                self._fetch_and_checkout(target, ref)
                return target
            self._rebuild(target, url=auth_url, ref=ref)
        return target

    def _rebuild(self, target: Path, *, url: str, ref: str) -> None:
        """把残缺/无效的缓存工作树重建好：清掉重 clone，删不净则复用/明确报错。

        半途被中断的 clone 会留下只有 hooks/info 的残缺 `.git`——此前
        `shutil.rmtree(..., ignore_errors=True)` 遇文件被占用/只读会静默残留非空目录，
        后续 `git clone` 便报误导性的 ``already exists and is not an empty directory``。
        这里改为：先尽力删干净（含只读文件转可写），删不净即视为被并发进程占用——
        若占用方已把仓库整理有效则顺势增量同步复用，否则抛清晰的"占用"错误，
        不再让 git clone 报那条误导信息。
        """
        self._force_remove(target)
        if _is_non_empty(target):
            # 删不净 = 正被占用。能判定它是有效仓库就复用，避免重复 clone 竞争。
            if self._valid_repo(target):
                logger.warning("缓存仓库 %s 被占用但状态有效，改走增量同步", target)
                self._reset_remote(target, url)
                self._fetch_and_checkout(target, ref)
                return
            raise RuntimeError(
                f"缓存仓库 {target} 正被其他进程占用（残留文件无法删除），无法重建"
            )
        self._clone(url, target)

    def _force_remove(self, target: Path) -> None:
        """删除 `target`（含 Windows 只读文件）；删不净则静默保留现场，交由调用方判定。"""
        def _onerror(func, path, _exc_info):  # noqa: ANN001
            # 只读属性（Windows 上 rmtree 对只读文件会失败）→ 转可写后重试一次；
            # 仍失败（真被占用锁定）则留给上层判定，而非被 ignore_errors 静默吞掉。
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except OSError:
                pass

        if target.exists():
            shutil.rmtree(target, onerror=_onerror)

    def _valid_repo(self, target: Path) -> bool:
        """判断 `target` 是否完好的 git 工作树。

        只判 `.git` 存在不够——被中断的 clone 会留下只有 hooks/info 的残缺 `.git`，
        仍能被 `exists()` 命中，却在后续 `git fetch` 时报 ``not a git repository``。
        这里让 git 自行识别，识别不了即判定无效。
        """
        if not (target / ".git").exists():
            return False
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=target, check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _clone(self, url: str, target: Path) -> None:
        _run(["git", "clone", url, str(target)], timeout=self.timeout)

    def _reset_remote(self, target: Path, auth_url: str) -> None:
        # 已 cache 的仓库可能无凭据 / token 更新：改写 origin 让后续 fetch 走新版。
        try:
            proc = subprocess.run(
                ["git", "remote", "get-url", "origin"], cwd=target, check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return
        if proc.returncode != 0:
            return
        existing = (proc.stdout or "").strip()
        if existing != auth_url or _contains_token(existing):
            _run(["git", "remote", "set-url", "origin", auth_url], cwd=target, timeout=30)

    def _fetch_and_checkout(self, target: Path, ref: str) -> None:
        _run(["git", "fetch", "--all", "--prune"], cwd=target, timeout=self.timeout)
        is_sha = bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", ref))
        checkout_ref = ref if is_sha else f"origin/{ref}"
        _run(["git", "reset", "--hard", checkout_ref], cwd=target, timeout=self.timeout)


def _contains_token(url: str) -> bool:
    """判断 clone URL 是否已带 userinfo 凭据（避免无谓改写，也让 `_reset_remote` 可读）。"""
    return "@" in (urlparse(url).netloc or "")


def _is_non_empty(path: Path) -> bool:
    """目录存在且非空。git clone 只拒绝「已存在且非空」的目标，空目录允许直接写入。"""
    return path.is_dir() and any(path.iterdir())


# 供运行时以 async 方式在后台线程跑 git（不阻塞事件循环）。
TokenProvider = Callable[[str], Awaitable[str | None]]

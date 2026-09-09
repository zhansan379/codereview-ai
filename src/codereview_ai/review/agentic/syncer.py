"""仓库同步：把被审仓库 clone 到本地做 agent 全仓上下文（DESIGN §12.1 补齐）。

借鉴 AI-Codereview-Gitlab `biz/agent/repo_syncer.py` 的机制，但**只取其 git 只读同步逻辑**：
- 懒 **bare clone**（首次）+ 增量 `fetch --all --prune`（**无 working tree、无 reset**）；
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
    """把远程仓库惰性 bare clone 到 `cache_root`，之后增量同步到目标 ref。

    每个 repo 一个子目录：`cache_root/<safe_key>/`（**bare 仓库**，无 working tree）
    + 一个 .lock 文件。cache 跨审查复用：下次直接 `fetch --all --prune` 到位，
    不重复 clone。agent 以 ref(sha) 寻址对象库读取，无需物化工作树。
    """

    def __init__(self, cache_root: Path | str, *, timeout: int = 300) -> None:
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    def sync_to(self, *, url: str, key: str, ref: str, token: str = "") -> Path:
        """确保 `key` 仓库本地可用且 `ref` 对象已入库，返回 bare 仓库目录。

        bare 仓库无 working tree：读由上层以 `ref`(sha) 寻址 git 对象完成。`ref`
        若是 7-40 位十六进制则额外确保该 commit 已 fetch 到位；否则仅保持仓库新鲜。
        """
        if not url:
            raise RuntimeError("缺少 clone URL")
        target = self.cache_root / slugify_key(key)
        lock_path = self.cache_root / f"{slugify_key(key)}.lock"
        auth_url = _auth_url(url, token)
        with _FileLock(lock_path):
            if self._valid_repo(target):
                self._reset_remote(target, auth_url)
                self._fetch_and_check(target, ref)
                return target
            self._rebuild(target, url=auth_url, ref=ref)
        return target

    def remove(self, key: str) -> None:
        """删除 `key` 对应的缓存仓库目录（含 Windows 只读处理）。

        复用 lock + `_force_remove` 语义；被并发 fetch 占用删不净则抛 `RuntimeError`
        （调用方据此保留 DB 行、返回占用错误），不静默残留。
        """
        slug = slugify_key(key)
        target = self.cache_root / slug
        lock_path = self.cache_root / f"{slug}.lock"
        with _FileLock(lock_path):
            self._force_remove(target)
        if _is_non_empty(target):
            raise RuntimeError(f"缓存仓库 {slug} 正被占用，删除失败")
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass  # 锁文件删不动无所谓，下次接管

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
                self._fetch_and_check(target, ref)
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
        """判断 `target` 是否完好的 bare 仓库（git 目录即 `target` 本身）。

        裸仓库没有 `.git` 子目录，直接让 git 自认：`rev-parse --is-bare-repository`
        输出 ``true`` 才算有效对象库；被中断的 clone 或纯残留目录识别不了即无效。
        """
        if not target.is_dir():
            return False
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--is-bare-repository"], cwd=target, check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def _clone(self, url: str, target: Path) -> None:
        _run(["git", "clone", "--bare", url, str(target)], timeout=self.timeout)

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

    def _fetch_and_check(self, target: Path, ref: str) -> None:
        """增量 fetch 到最新，并确保 `ref`（若是 7-40 位 hex）对象已入库。

        bare 仓库无 working tree 可 `reset --hard`——读全走 git 对象，因此这里只负责
        让对象/refs 新鲜。head 若是远端临时分支已删、不在任何已拉 ref 可达历史的 sha，
        补 `git fetch origin <sha>`（服务端允许 reachable 时有效）；仍缺失则抛错，
        由上层降级为普通 diff 审查（等价旧 `reset --hard` 失败降级）。
        """
        _run(["git", "fetch", "--all", "--prune"], cwd=target, timeout=self.timeout)
        is_sha = bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", ref))
        if is_sha and not self._object_present(target, ref):
            try:
                _run(["git", "fetch", "origin", ref], cwd=target, timeout=self.timeout)
            except RuntimeError:
                pass  # 服务端可能拒绝按 sha fetch，交由下方存在性校验定夺
            if not self._object_present(target, ref):
                raise RuntimeError(f"目标 commit {ref} 在 fetch 后仍不可达")

    @staticmethod
    def _object_present(target: Path, ref: str) -> bool:
        """`git cat-file -t <ref>` 判定对象是否已在库（commit 即视为可达）。"""
        try:
            proc = subprocess.run(
                ["git", "-C", str(target), "cat-file", "-t", ref], check=False,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
        return proc is not None and proc.returncode == 0 and bool((proc.stdout or "").strip())


def _contains_token(url: str) -> bool:
    """判断 clone URL 是否已带 userinfo 凭据（避免无谓改写，也让 `_reset_remote` 可读）。"""
    return "@" in (urlparse(url).netloc or "")


def _is_non_empty(path: Path) -> bool:
    """目录存在且非空。git clone 只拒绝「已存在且非空」的目标，空目录允许直接写入。"""
    return path.is_dir() and any(path.iterdir())


# 供运行时以 async 方式在后台线程跑 git（不阻塞事件循环）。
TokenProvider = Callable[[str], Awaitable[str | None]]

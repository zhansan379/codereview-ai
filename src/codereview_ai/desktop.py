"""桌面版（exe 单机包）启动器。

PyInstaller 打包后的用户预期是「双击 exe 就能用」，相比 Docker 常驻部署，启动器
多管几件事（源码下 `python -m codereview_ai.desktop` 走同一套逻辑，便于冒烟）：

1. 数据目录：exe/源码根可写就用（便携模式），不可写（如 Program Files）落
   %LOCALAPPDATA%/codereview-ai；工作目录固定到此，`.env`（密钥）与 `./data`
   （SQLite，Settings 默认 `sqlite:///./data/app.db`）都归这里。
2. 密钥引导：REQUIRED_SECRETS 缺哪枚生成哪枚写进 .env——首次双击即用，
   不再触发 fail-fast 退出；已配过的键（用户手工或旧版）原样保留。
3. 随包资源：PyInstaller 解包目录（sys._MEIPASS）里的 frontend/dist 与 ruff-bin
   通过 CR_FRONTEND_DIST / PATH 注册进运行时；缺失时与 Docker 行为一致——
   /admin 404、静态层 warning 降级，服务本体不受影响。
4. 常驻 + 自动开浏览器：uvicorn 前台常驻（关窗即停，Ctrl+C 优雅退出），
   /health 就绪后自动打开 /admin/。
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet

#: 各密钥的生成器（与 REQUIRED_SECRETS 注释里的生成命令一一对应，键为 env 名）
_GENERATORS: dict[str, Callable[[], str]] = {
    "CR_SECRET_KEY": lambda: secrets.token_urlsafe(48),
    "CR_WEBHOOK_SECRET": lambda: secrets.token_urlsafe(48),
    "CR_ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(),
    "CR_ADMIN_PASSWORD": lambda: secrets.token_urlsafe(24),
}


def _writable(d: Path) -> bool:
    try:
        probe = d / ".cr-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def pick_app_dir() -> Path:
    """数据目录：exe（冻结态）/ 当前目录（源码态）可写就用，否则落 LOCALAPPDATA。"""
    if getattr(sys, "frozen", False):
        home = Path(sys.executable).resolve().parent
    else:
        home = Path.cwd()
    if _writable(home):
        return home
    base = os.environ.get("LOCALAPPDATA")
    target = (Path(base) if base else Path.home() / ".codereview-ai") / "codereview-ai"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _read_env_keys(path: Path) -> set[str]:
    """现有 .env里已配置的密钥键名（只认键名，值格式不校验——留给 Settings 校验）。"""
    if not path.is_file():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip().split("=", 1)[0].strip()
        if name in _GENERATORS:
            keys.add(name)
    return keys


def ensure_env(app_dir: Path) -> tuple[Path, list[str]]:
    """缺哪枚 REQUIRED_SECRETS 生成哪枚追加进 .env；返回 (路径, 本次新生成的键)。

    只追加不重写：已有行（含用户手改的值）原样保留，幂等——第二次调用返回空列表。
    """
    env_path = app_dir / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    have = _read_env_keys(env_path)
    missing = [env for env in _GENERATORS if env not in have]
    if not missing:
        return env_path, []

    if existing:
        lines = existing.splitlines()
        if lines[-1].strip():
            lines.append("")  # 追加前补空行，人手编辑过的 .env 更好读
    else:
        lines = ["# codereview-ai 桌面版自动生成的密钥；删除本文件后重启 exe 可重新生成。"]
    for env in missing:
        lines.append(f"{env}={_GENERATORS[env]()}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path, missing


def env_value(env_path: Path, key: str) -> str:
    """读 .env 里某键的值（不存在返回空串）；用于启动时把生成的密码报给用户。"""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.strip().partition("=")
        if name == key:
            return value
    return ""


def resolve_frontend_dist() -> Path | None:
    """管理台产物：冻结态在解包目录，源码态在仓库根；缺失返回 None（与容器一致降级）。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        cand = (Path(meipass) if meipass else Path.cwd()) / "frontend" / "dist"
    else:
        cand = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return cand if (cand / "index.html").is_file() else None


def prepend_bundled_tools() -> None:
    """随包工具（ruff）插到 PATH 最前，静态分析层按名字在 PATH 找 `ruff`。

    解包目录没有 ruff-bin 就跳过——静态层自动 warning 降级，与容器无 semgrep 一致。
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", "")
    tools = (Path(meipass) if meipass else Path.cwd()) / "ruff-bin"
    if tools.is_dir():
        os.environ["PATH"] = f"{tools}{os.pathsep}{os.environ.get('PATH', '')}"


def port_free(host: str, port: int) -> bool:
    """起服务前试绑一次：Windows 下 uvicorn 绑不上会直接退出，先给句人话。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _open_browser_when_ready(base_url: str, timeout: float = 30.0) -> None:
    """后台线程等 /health 就绪再开浏览器；开不了也不影响服务（daemon 线程）。"""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                webbrowser.open(f"{base_url}/admin/")
                return
        except httpx.HTTPError:
            time.sleep(0.5)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="codereview-ai", description="codereview-ai 桌面版：本地常驻服务 + 管理台"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；webhook 需跨机回调时用 0.0.0.0（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=5001, help="监听端口（默认 5001）")
    parser.add_argument("--no-browser", action="store_true", help="就绪后不自动打开浏览器")
    args = parser.parse_args(argv)

    # 非 UTF-8 控制台（如 GBK）下中文打印的兜底，避免生僻字符直接崩
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    app_dir = pick_app_dir()
    os.chdir(app_dir)
    (app_dir / "data").mkdir(exist_ok=True)

    env_path, generated = ensure_env(app_dir)
    if generated:
        print(f"[codereview-ai] 首次启动，已生成缺失密钥 → {env_path}")
        if "CR_ADMIN_PASSWORD" in generated and not os.environ.get("CR_ADMIN_PASSWORD"):
            pwd = env_value(env_path, "CR_ADMIN_PASSWORD")
            print(f"[codereview-ai] 后台登录密码（请记下，也存于 .env）：{pwd}")

    dist = resolve_frontend_dist()
    if dist is not None:
        os.environ["CR_FRONTEND_DIST"] = str(dist)
    prepend_bundled_tools()

    base = f"http://{args.host}:{args.port}"
    if not port_free(args.host, args.port):
        print(f"[codereview-ai] 端口 {args.port} 已被占用（也许服务已在跑）。换端口：--port 5002")
        raise SystemExit(1)

    print(f"[codereview-ai] 数据目录：{app_dir}")
    print(f"[codereview-ai] 管理台：{base}/admin/   （关闭本窗口 = 停止服务）")
    if args.host == "127.0.0.1":
        print("[codereview-ai] 提示：webhook 需要跨机回调时，改用 --host 0.0.0.0 重启。")

    import codereview_ai.main  # noqa: F401  触达 app 模块，确保 PyInstaller 把它收进包

    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(base,), daemon=True).start()

    uvicorn.run("codereview_ai.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    run()

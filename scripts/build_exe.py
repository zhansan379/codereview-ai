"""构建桌面单机版（Windows exe）。

步骤：前端产物（缺才建）→ uv 同步（含 desktop 组）→ pyinstaller 跑 desktop.spec。
用法：uv run python scripts/build_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], **kw: Any) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def main() -> None:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        npm = shutil.which("npm")
        if not npm:
            raise SystemExit("frontend/dist 缺失且找不到 npm：先在 frontend/ 下手动 npm run build")
        print("frontend/dist 缺失，先构建前端……")
        _run([npm, "run", "build"], cwd=ROOT / "frontend")

    _run(["uv", "sync", "--group", "desktop"])
    _run(["uv", "run", "pyinstaller", "--noconfirm", "desktop.spec"])

    out = ROOT / "dist" / "codereview-ai"
    print(f"\n完成：{out / 'codereview-ai.exe'}（分发整个 {out} 目录）")


if __name__ == "__main__":
    main()

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：codereview-ai 桌面单机版（Windows，onedir）。

构建：uv run python scripts/build_exe.py（或手动 uv sync --group desktop && uv run pyinstaller --noconfirm desktop.spec）
产物：dist/codereview-ai/codereview-ai.exe——整个目录分发（onedir 启动快、杀软误报少，
     onefile 每次启动解包几百 MB，冷启动以十秒计，不采用）。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = ["codereview_ai.main"]

# litellm / tiktoken 动态 import 极多（厂商插件、tiktoken_ext 编码器按名字加载），
# 不全收就是运行期 ModuleNotFoundError；apscheduler 的触发器/执行器也走动态路径
for pkg in ("litellm", "tiktoken", "apscheduler"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# SQLAlchemy 方言按 URL scheme 动态加载（默认 sqlite+aiosqlite；切 PG 时 postgresql+asyncpg），
# 方言模块和 DBAPI 驱动（aiosqlite/asyncpg，项目源码零静态 import）都要显式收，
# 否则启动建引擎时 ModuleNotFoundError（实测踩过：缺 aiosqlite）
hiddenimports += collect_submodules("sqlalchemy.dialects")
for pkg in ("aiosqlite", "asyncpg"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 项目自带数据：review/semgrep_rules/*.yml（wheel 里靠 hatch force-include，这里手动收）
datas += collect_data_files("codereview_ai")

# 前端管理台产物（启动器把 CR_FRONTEND_DIST 指到这里；dist 不进 git，先 npm run build）
_frontend = os.path.join(SPECPATH, "frontend", "dist")
if not os.path.isfile(os.path.join(_frontend, "index.html")):
    raise SystemExit("缺 frontend/dist：先在 frontend/ 下执行 npm run build")
datas += [(_frontend, "frontend/dist")]

# ruff 单文件二进制（静态分析层按名字在 PATH 找 `ruff`；dev 组已装，取 venv Scripts 里的）
_ruff = os.path.join(os.path.dirname(sys.executable), "ruff.exe")
if os.path.isfile(_ruff):
    datas += [(_ruff, "ruff-bin")]
else:
    print("警告：venv 里没找到 ruff.exe，打包出的 exe 静态分析 ruff 层不可用（semgrep 本就不附带）")

a = Analysis(
    ["scripts/desktop_launcher.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir：二进制留给 COLLECT 归拢
    name="codereview-ai",
    debug=False,
    console=True,  # 常驻服务需要窗口看日志/收 Ctrl+C，不做无边框
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # UPX 压缩易被杀软误报，不值当
    name="codereview-ai",
)

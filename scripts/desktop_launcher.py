"""PyInstaller 桌面版入口（薄壳）：全部逻辑在 codereview_ai.desktop.run。

打包配置见 desktop.spec；构建命令见 scripts/build_exe.py。
"""

from codereview_ai.desktop import run

if __name__ == "__main__":
    run()

# 桌面单机版（Windows exe）

不想装 Docker 的用户可以直接用桌面单机版：一个 zip，解压双击 `codereview-ai.exe`，
本地起服务 + 自动打开管理台。与 Docker 版功能一致（webhook 审查、补拉、日报、推送都支持），
数据存本地 SQLite。

## 使用

1. 下载 `codereview-ai-windows-x64.zip`（GitHub Release，或 CI Artifacts），解压到任意目录。
2. 双击 `codereview-ai.exe`，控制台窗口保持开着（关窗 = 停止服务）。
3. 首次启动会自动生成四枚密钥写进数据目录的 `.env`，并在控制台打印**后台登录密码**
   （同存于 `.env` 的 `CR_ADMIN_PASSWORD`）。浏览器自动打开 `http://127.0.0.1:5001/admin/`，
   密码登录即用。

数据目录的选择规则：exe 所在目录可写就直接用（便携模式，`.env` 和 `data/` 都在 exe 旁）；
不可写（比如放在 Program Files）则退到 `%LOCALAPPDATA%\codereview-ai`。控制台会打印实际位置。

## 常用参数

```text
codereview-ai.exe --port 5002        # 换端口（默认 5001，被占用会提示）
codereview-ai.exe --host 0.0.0.0     # 接收 GitLab/GitHub/Gitee 跨机 webhook 回调
codereview-ai.exe --no-browser       # 启动后不自动开浏览器
```

webhook 回调地址填 `http://<这台机器的IP>:5001/api/webhook/<平台>`，Secret 用 `.env` 里的
`CR_WEBHOOK_SECRET`（或后台「设置」页查看）。模型、平台 token 等都在管理台「设置」页配。

## 静态分析层的差异

- **ruff**：已随包附带（`ruff-bin/`），Python 文件的静态检查开箱即用。
- **semgrep**：体积过大不随包。需要时在机器上 `pip install semgrep`，保证 `semgrep`
  在 PATH 上即可，重启 exe 自动生效；没有它只是静态层少一路，warning 降级不影响审查。

## 升级与卸载

- 升级：下载新 zip，覆盖 exe 所在目录（数据在数据目录的 `.env` 和 `data/` 里，不受影响）。
- 卸载：删解压目录；若数据目录在 LOCALAPPDATA（非便携模式），连 `%LOCALAPPDATA%\codereview-ai` 一起删。

## 开发者：构建

```bash
uv run python scripts/build_exe.py
# 产物：dist/codereview-ai/codereview-ai.exe（整个 dist/codereview-ai 目录分发）
```

脚本会按需构建前端产物（`frontend/dist` 不进 git）、`uv sync --group desktop` 安装
PyInstaller、再跑 `desktop.spec`。spec 里已处理 litellm/tiktoken/apscheduler 的动态
import 收集（`collect_all`）和 semgrep 规则、前端产物、ruff 二进制的附带。

打 `v*` tag 推送会触发 `.github/workflows/desktop.yml`，在 windows-latest 上构建并把
`codereview-ai-windows-x64.zip` 挂到 GitHub Release（也可在 Actions 页手动 workflow_dispatch 触发）。

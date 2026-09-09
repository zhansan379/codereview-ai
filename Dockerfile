# codereview-ai 运行时镜像（M6 标准 9）。
# python:3.11-slim + uv 同步运行时依赖，非 root 运行，HEALTHCHECK 探 /health。
# 真实 `docker compose up` 冒烟见 docs/M6_SMOKE.md。

FROM python:3.11-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# uv 二进制（从官方镜像拷出，避免 curl 安装脚本）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 先拷依赖清单让镜像层缓存（改源码不重装依赖）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 拷源码并完成安装
COPY src ./src
RUN uv sync --frozen --no-dev

# 拷前端构建产物（/admin SPA；mount_admin 找不到 frontend/dist 时后台页 404，见 admin_ui.py）
COPY frontend/dist ./frontend/dist

# semgrep：静态分析层（DESIGN §11）在 PATH 上找 `semgrep` 二进制。它不在项目
# dependencies（是独立 CLI，subprocess 调用），故单独 pip 装——必须在切到 appuser 之前装，
# 否则 appuser 无写权限；规则用的是内置本地包（semgrep_rules/，随 COPY src 进镜像，离线可用）。
RUN pip install --no-cache-dir semgrep

# 非 root 运行，降低容器被攻破后的影响面
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

# 数据库文件目录（compose 挂载命名卷）
RUN mkdir -p /app/data

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5001/health').status==200 else 1)"

# uvicorn 走 __getattr__ 惰性构建 app；缺密钥时 Settings fail-fast 退出并提示生成命令
CMD ["uv", "run", "uvicorn", "codereview_ai.main:app", "--host", "0.0.0.0", "--port", "5001"]
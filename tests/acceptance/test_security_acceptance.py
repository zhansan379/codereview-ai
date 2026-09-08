"""M6 验收套件 · 安全侧（PRD 标准 11 / 12 / 13）。

- 标准 11：未配置 SECRET_KEY → 拒绝启动（SystemExit）并给出生成命令。
- 标准 12：日志/掩码中检索不到任何 token / api_key 明文。
- 标准 13：自动扫 src 对照 reference/antipatterns.md 的强信号（无 shell=True、
  verify=False、裸 requests.；签名统一 hmac.compare_digest）。逐条 A1-C5 人工勾选表
  见 docs/M6_SMOKE.md。
"""

from __future__ import annotations

import logging
from io import StringIO

import pytest

from codereview_ai.logging import JsonFormatter, SensitiveFilter, mask

# ── 标准 11：未配置 SECRET_KEY 拒绝启动并提供生成命令 ──────────────────────


def test_c11_missing_secret_rejects_startup_with_generate_command():
    from codereview_ai.config.settings import Settings

    with pytest.raises(SystemExit) as excinfo:
        Settings(secret_key="", webhook_secret="", encryption_key="", admin_password="")
    msg = str(excinfo.value)
    assert "CR_SECRET_KEY" in msg  # 指出缺失项
    assert "token_urlsafe" in msg  # 给出生成命令


# ── 标准 12：日志中检索不到 token / api_key 明文 ──────────────────────────


def test_c12_mask_function_redacts_tokens():
    sample = ("Authorization: Bearer sek123 "
              "https://chanel/webhook?sign=s1&token=t1 "
              "api_key=sk-live webhook_secret=qwe key:k2")
    out = mask(sample)
    for lit in ("sek123", "s1", "t1", "sk-live", "qwe", "k2"):
        assert lit not in out
    assert "[REDACTED]" in out


def test_c12_log_handler_redacts_tokens():
    # 独立 logger：不污染全局日志装配
    logger = logging.getLogger("codereview_ai.acceptance")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SensitiveFilter())
    logger.addHandler(handler)

    logger.info("调用 LLM uses api_key=%s webhook_secret=%s Authorization: Bearer %s",
                "sk-real", "wh-real", "tok-real")
    out = buf.getvalue()

    for lit in ("sk-real", "wh-real", "tok-real"):
        assert lit not in out, f"日志泄漏明文: {lit}"
    assert "[REDACTED]" in out


# ── 标准 13：对照 antipatterns.md 自动扫描强信号 ───────────────────────────


def test_c13_no_reproduced_antipatterns_in_src():
    import pathlib

    root = pathlib.Path("src") / "codereview_ai"
    banned = ("shell=True", "verify=False", "requests.", "urllib.request")
    hits: list[str] = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for tok in banned:
            if tok in text:
                hits.append(f"{p}: {tok}")
    assert hits == [], "存在 antipatterns.md 复现信号:\n" + "\n".join(hits)

    # A1：签名校验统一走 hmac.compare_digest（安全比对，无 == 明文比较）
    sig = (root / "forges" / "signatures.py").read_text(encoding="utf-8")
    assert "compare_digest" in sig


def test_c13_agentic_has_no_shell_tool():
    # A2：agentic **暴露给 LLM 的工具**（tools.py）不提供 shell/任意执行，攻击面约等于零。
    # 扫描范围限定 agents 可调用的工具表面 `tools.py`；`syncer.py` 是 clone 初始化期的
    # git plumbing（固定命令、无 shell=True、LLM 输入绝不进入命令行），不属于可调工具。
    import pathlib
    import re

    tools_path = pathlib.Path("src/codereview_ai/review/agentic/tools.py")
    code = tools_path.read_text(encoding="utf-8")
    # 去掉 docstring 与行注释里的措辞（如"无 run_command"的说明），只看可执行代码
    code = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', " ", code, flags=re.S)
    code = re.sub(r"(?m)^\s*#.*$", "", code)
    for tok in ("run_command", "subprocess", "shell=True"):
        assert tok not in code, f"agentic 可调用工具 surface 出现可执行 {tok}"

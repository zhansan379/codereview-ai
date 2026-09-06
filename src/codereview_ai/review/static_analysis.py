"""静态分析融合（DESIGN §11）：ruff + semgrep → 归一化 Finding(source='static:*')。

- 只对**变更涉及的文件**跑（design 决定：全仓跑太慢），用 `FileDiff.new_file_content`
  物化一个临时工作区即可，无需 checkout。
- 按扩展名分发：`.py` → `ruff check --output-format json`；`js/ts` → eslint
  （未装则 warning 降级）；全部语言 → `semgrep --config p/ci --json`。
- 归一化：`(severity, file, line, title)` → `Finding(source='static:ruff'|'static:semgrep')`。
- 顺序与降级：静态分析**先跑**；任何一步失败只 warning 降级（返回空），绝不阻断审查主流程。
- 工具执行器 `runner` 以参数注入：离线测试传 fake runner 返回罐头 JSON，线上默认
  `SubprocessRunner` 用 asyncio subprocess 调用真实二进制；二进制缺失也降级为空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codereview_ai.domain.models import Category, FileDiff, Finding, Severity

logger = logging.getLogger("codereview_ai.static_analysis")

#: 参与 ruff 分析的扩展名；其余交给 semgrep 兜底。
_RUFF_EXTS = frozenset({"py", "pyi"})
#: eslint 目标扩展名（本轮未装 node 链路，装没装都降级处理）。
_ESLINT_EXTS = frozenset({"js", "jsx", "ts", "tsx"})


@dataclass
class RunResult:
    """一次工具调用的产物：退出码 + stdout 文本（ruff 有 finding 时退出码为 1 但 stdout 仍合法 JSON）。"""  # noqa: E501

    stdout: str
    exit_code: int


class StaticRunner(Protocol):
    """工具执行协议：`run(tool, args, cwd)` → (stdout, exit_code)。

    真实实现走 subprocess；测试注入 fake 返回罐头 JSON。`OSError`（二进制缺失）
    由调用方捕获按降级处理。
    """

    async def run(self, tool: str, args: list[str], cwd: Path) -> RunResult: ...


class SubprocessRunner:
    """默认实现：用 asyncio.create_subprocess_exec 调用系统里的 ruff/semgrep。"""

    async def run(self, tool: str, args: list[str], cwd: Path) -> RunResult:
        proc = await asyncio_create_subprocess(tool, args, cwd)
        stdout, _stderr = await proc.communicate()
        return RunResult(stdout.decode("utf-8", errors="replace"), proc.returncode or 0)


async def asyncio_create_subprocess(
    tool: str, args: list[str], cwd: Path
) -> asyncio.subprocess.Process:
    """subprocess 封装：独立函数便于离线测试替换（避免直接 import asyncio.subprocess）。"""
    return await asyncio.create_subprocess_exec(
        tool, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )


# ── ruff 归一化 ─────────────────────────────────────────────────────────


def _ruff_prefix(code: str) -> str:
    """取规则前缀的字母段：`F401`→`F`、`ASYNC113`→`ASYNC`、`RUF001`→`RUF`。"""
    head = (code.split("/", 1)[0] if "/" in code else code).split("(")[0].upper()
    return "".join(ch for ch in head if ch.isalpha())


def _ruff_severity(code: str) -> Severity:
    """按规则前缀粗判严重级：S(安全)偏重，F/E/B/ASYNC 中，其余低。"""
    p = _ruff_prefix(code)
    if p == "S":
        return Severity.HIGH
    if p in {"F", "E", "B", "ASYNC", "P1"}:
        return Severity.MEDIUM
    return Severity.LOW


def _ruff_category(code: str) -> Category:
    p = _ruff_prefix(code)
    if p == "S":
        return Category.SECURITY
    if p == "F":
        return Category.BUG
    if p in {"E", "W"}:
        return Category.STYLE
    if p in {"B", "ASYNC", "RUF"}:
        return Category.MAINTAINABILITY
    if p == "T":
        return Category.TEST
    return Category.OTHER


def _parse_ruff(stdout: str) -> list[Finding]:
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    out: list[Finding] = []
    for it in payload:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "")
        loc = it.get("location") or {}
        row = int(loc.get("row") or it.get("line") or 0)
        if row <= 0:
            continue
        out.append(Finding(
            content=str(it.get("message") or code),
            category=_ruff_category(code),
            severity=_ruff_severity(code),
            existing_code="",
            file=str(it.get("filename") or ""),
            line=row,
            old_line=None,
            side="RIGHT",
            source="static:ruff",
        ))
    return out


# ── semgrep 归一化 ──────────────────────────────────────────────────────


def _semgrep_severity(sev: str) -> Severity:
    s = (sev or "").upper()
    if s in {"ERROR", "CRITICAL"}:
        return Severity.HIGH
    if s == "WARNING":
        return Severity.MEDIUM
    return Severity.LOW


def _semgrep_category(check_id: str, message: str) -> Category:
    blob = f"{check_id} {message}".lower()
    if any(k in blob for k in ("security", "injection", "xss", "sql", "ssti", "command", "secret")):
        return Category.SECURITY
    if any(k in blob for k in ("performance", "denial", "timeout", "complexity")):
        return Category.PERFORMANCE
    return Category.OTHER


def _parse_semgrep(stdout: str) -> list[Finding]:
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    out: list[Finding] = []
    for it in results:
        if not isinstance(it, dict):
            continue
        extra = it.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        start = it.get("start") or {}
        if not isinstance(start, dict):
            continue
        row = int(start.get("line") or 0)
        if row <= 0:
            continue
        sev = str(extra.get("severity") or "WARNING")
        msg = str(extra.get("message") or it.get("check_id") or "semgrep")
        cid = str(it.get("check_id") or "")
        out.append(Finding(
            content=msg,
            category=_semgrep_category(cid, msg),
            severity=_semgrep_severity(sev),
            existing_code="",
            file=str(it.get("path") or ""),
            line=row,
            old_line=None,
            side="RIGHT",
            source="static:semgrep",
        ))
    return out


# ── 工作区物化 + 入口 ───────────────────────────────────────────────────


def materialize_workspace(diffs: list[FileDiff], root: Path) -> list[str]:
    """把每个变更文件的新内容写进临时工作区，返回写出的相对路径列表。

    纯新增/修改文件才有 `new_file_content`；纯删除（new_path='/dev/null'）无内容，跳过。
    """
    written: list[str] = []
    for d in diffs:
        if not d.new_file_content:
            continue
        rel = (d.new_path or "").removeprefix("/")  # 防绝对路径逃逸
        if not rel or rel == "dev/null":
            continue
        target = root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(d.new_file_content, encoding="utf-8")
        except OSError:
            logger.warning("静态分析物化 %s 失败，跳过该文件", rel)
            continue
        written.append(rel)
    return written


def render_static_findings(findings: list[Finding], files: set[str] | None = None) -> str:
    """把静态 findings 渲染成注入 prompt 的提示文本；`files` 过滤到对应的文件组。

    空集返回空串（上层对空串不加注入块）。
    """
    sel = [f for f in findings if files is None or (f.file or "") in files]
    if not sel:
        return ""
    lines = ["以下问题已由静态分析工具发现，会**单独呈现**，请勿重复报告：", ""]
    for f in sel:
        loc = f"{f.file}:{f.line}" if f.line is not None else (f.file or "")
        lines.append(f"- [{f.severity}] [{f.category}] {loc}: {f.content}")
    return "\n".join(lines)


class StaticAnalyzer:
    """静态分析编排：物化工作区 → 按语言分发 ruff/semgrep → 归一化 → 返回 Finding 列表。"""

    def __init__(
        self,
        runner: StaticRunner | None = None,
        workspace: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self._workspace = workspace
        self._own_workspace = workspace is None
        self.enabled = enabled

    async def analyze(self, diffs: list[FileDiff]) -> list[Finding]:
        """对变更文件跑静态分析，返回归一化 findings；任何失败降级为空。"""
        if not self.enabled or not diffs:
            return []
        root = self._workspace or Path(tempfile.mkdtemp(prefix="cr-static-"))
        result: list[Finding] = []
        try:
            written = materialize_workspace(diffs, root)
            if not written:
                return []
            py_files = sorted(
                p for p in written if p.rsplit(".", 1)[-1].lower() in _RUFF_EXTS
            )
            if py_files:
                result += await self._run_ruff(root)
            result += await self._run_semgrep(root)
            return result
        except Exception as exc:  # noqa: BLE001 —— 静态分析失败必须降级，绝不可阻断主链
            logger.warning("静态分析整体降级：%s", exc)
            return []
        finally:
            if self._own_workspace and self._workspace is None:
                import shutil

                shutil.rmtree(root, ignore_errors=True)

    async def _run_ruff(self, root: Path) -> list[Finding]:
        try:
            res = await self.runner.run("ruff", ["check", "--output-format", "json", "."], cwd=root)
        except OSError:
            logger.warning("静态分析：ruff 未安装，跳过（降级）")
            return []
        findings = _parse_ruff(res.stdout)
        logger.info("静态分析 ruff：%d 条", len(findings))
        return findings

    async def _run_semgrep(self, root: Path) -> list[Finding]:
        try:
            res = await self.runner.run(
                "semgrep", ["--config", "p/ci", "--json", "--strict", "."], cwd=root
            )
        except OSError:
            logger.warning("静态分析：semgrep 未安装，跳过（降级）")
            return []
        findings = _parse_semgrep(res.stdout)
        logger.info("静态分析 semgrep：%d 条", len(findings))
        return findings

"""静态分析融合（DESIGN §11）：ruff + semgrep → 归一化 Finding(source='static:*')。

- 只对**变更涉及的文件**跑（design 决定：全仓跑太慢），用 `FileDiff.new_file_content`
  物化一个临时工作区即可，无需 checkout。
- 按扩展名分发：`.py` → `ruff check --output-format json`；`js/ts` → eslint
  （未装则 warning 降级）；全部语言 → `semgrep --json`。
- semgrep 规则源分两档：
  - **默认 registry-first**：`--config p/ci`（云规则集，覆盖最广，联网**拉取一次**）。
    失败（无网/内网/规则损坏）→ 自动**降级到内置本地规则包**
    （`review/semgrep_rules/`，随 wheel 打包，离线可用）。
  - **显式** `CR_SEMGREP_RULES=本地目录`：直接用它，**完全离线**、不碰 registry。
  - `SubprocessRunner` 子进程 env 置 `SEMGREP_SEND_METRICS=off`，命令带
    `--disable-version-check`——默认位只保留「拉规则」这一次必要网络请求。
- 归一化：`(severity, file, line, title)` → `Finding(source='static:ruff'|'static:semgrep')`。
- 顺序与降级：静态分析**先跑**；任何一步失败只 warning 降级（返回空），绝不阻断审查主流程。
  其中 ruff 非零退出是「有 finding」的常态（不 warning）；semgrep 非零退出代表**装载失败**
  （含联网拉规则失败）→ 记 warning，但仍尽力解析已有结果，并按需降级内置本地包。
- 工具执行器 `runner` 以参数注入：离线测试传 fake runner 返回罐头 JSON，线上默认
  `SubprocessRunner` 用 asyncio subprocess 调用真实二进制；二进制缺失也降级为空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codereview_ai.domain.models import Category, FileDiff, Finding, Severity

logger = logging.getLogger("codereview_ai.static_analysis")


def _snippet(text: str, limit: int = 400) -> str:
    """日志用：截断长输出，避免一屏刷爆（只作降级提示，不承载逻辑）。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit} chars)"

#: 参与 ruff 分析的扩展名；其余交给 semgrep 兜底。
_RUFF_EXTS = frozenset({"py", "pyi"})
#: eslint 目标扩展名（本轮未装 node 链路，装没装都降级处理）。
_ESLINT_EXTS = frozenset({"js", "jsx", "ts", "tsx"})
#: 内置本地 semgrep 规则目录（离线，无需拉云仓库）。随 wheel 打包（pyproject force-include）。
_BUNDLED_RULES_DIR = Path(__file__).resolve().parent / "semgrep_rules"


@dataclass
class RunResult:
    """一次工具调用的产物：退出码 + stdout 文本（ruff 有 finding 时退出码为 1 但 stdout 仍合法 JSON）。

    `stderr` 供失败排查（如 semgrep 规则读取错误会写到 stderr），默认空串，不影响既有调用方。
    """  # noqa: E501

    stdout: str
    exit_code: int
    stderr: str = ""


@dataclass
class _SemgrepRun:
    """一次 semgrep 调用结果：findings + exit_ok（exit=0 视为装载成功）。

    exit_ok=False 表示该规则源没能成功装载（联网源=拉取失败），供上层决定兜底；
    但 findings 仍可能非空——semgrep 出错时往往也把已解析的结果打在 stdout 上。
    """

    findings: list[Finding]
    exit_ok: bool


class StaticRunner(Protocol):
    """工具执行协议：`run(tool, args, cwd)` → (stdout, exit_code)。

    真实实现走 subprocess；测试注入 fake 返回罐头 JSON。`OSError`（二进制缺失）
    由调用方捕获按降级处理。
    """

    async def run(self, tool: str, args: list[str], cwd: Path) -> RunResult: ...


class SubprocessRunner:
    """默认实现：用 asyncio.create_subprocess_exec 调用系统里的 ruff/semgrep。

    semgrep 默认上报匿名用量（`SEMGREP_SEND_METRICS`）。审查 worker 应把这类心跳关掉，
    让默认 registry-first 只产生「拉规则」这一次必要请求；这里在子进程 env 里显式关掉。
    """

    async def run(self, tool: str, args: list[str], cwd: Path) -> RunResult:
        env = {**os.environ, "SEMGREP_SEND_METRICS": "off"}
        proc = await asyncio_create_subprocess(tool, args, cwd, env=env)
        stdout, stderr = await proc.communicate()
        return RunResult(
            stdout.decode("utf-8", errors="replace"),
            proc.returncode or 0,
            stderr=stderr.decode("utf-8", errors="replace"),
        )


async def asyncio_create_subprocess(
    tool: str, args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> asyncio.subprocess.Process:
    """subprocess 封装：独立函数便于离线测试替换（避免直接 import asyncio.subprocess）。"""
    return await asyncio.create_subprocess_exec(
        tool, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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


def _relativize(finding: Finding, root: Path) -> None:
    """把 ruff/semgrep 输出的 `file` 归一化为仓库相对路径。

    工具以 `cwd=root` + 参数 `"."` 扫目录时，输出的 `filename`/`path` 是**绝对路径**
    （如 `$TMP/cr-static-xxx/src/a.py`），与 MR diff 的相对 `new_path` 对不上：详情页
    文件列显示一次性临时路径，且 `render_static_findings` 的注入过滤会失配。仅当路径为
    绝对且位于 `root` 内时转相对，其余（已是相对或跨盘符不可相对化）保持原样。
    """
    p = (finding.file or "").replace("\\", "/")
    if not os.path.isabs(p):
        return
    try:
        rel = os.path.relpath(p, root.resolve())
    except ValueError:  # 跨盘符无法计算相对路径（Windows）
        return
    if not rel.startswith(".."):  # 不相对化到 root 之外
        finding.file = rel.replace("\\", "/")


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
        semgrep_rules: Path | None = None,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self._workspace = workspace
        self._own_workspace = workspace is None
        self.enabled = enabled
        # 规则源语义：
        #   - semgrep_rules 为 None（CR_SEMGREP_RULES 未配）→ **默认 registry-first**：
        #     先试 `p/ci`（云规则集，覆盖广，联网拉取一次），失败自动降级内置离线包
        #     （`_BUNDLED_RULES_DIR`），保证无网/内网照常出结果。
        #   - 显式传入本地目录 → 直接用它（完全离线，不碰 registry；用户自决）。
        # 保留原始值（不 resolve 成 bundled），靠 `is None` 区分两条路径。
        self._semgrep_rules = semgrep_rules

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
            for f in result:
                _relativize(f, root)
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
        """semgrep：默认 registry-first，失败降级内置本地包；显式本地目录则直接用它。"""
        # 未显式配规则目录 → 先试 `p/ci`（云集，联网拉一次，覆盖最广）；失败再兜底内置离线包。
        primary = (
            str(self._semgrep_rules.resolve())
            if self._semgrep_rules is not None else "p/ci"
        )
        first = await self._run_semgrep_once(primary, root)
        if first.exit_ok:
            logger.info("静态分析 semgrep：%d 条", len(first.findings))
            return first.findings

        bundled = str(_BUNDLED_RULES_DIR.resolve())
        if primary != bundled:  # 主源（registry 或用户目录）失败 → 内置本地包兜底，保证离线可用
            logger.warning("静态分析：semgrep 主规则源(%s)失败，降级为内置本地规则", primary)
            fallback = await self._run_semgrep_once(bundled, root)
            logger.info("静态分析 semgrep（本地兜底）：%d 条", len(fallback.findings))
            return fallback.findings
        return first.findings  # 主源本就是内置且失败（未装/损坏）→ 直接返回

    async def _run_semgrep_once(self, config: str, root: Path) -> _SemgrepRun:
        """按给定 `--config` 跑一次 semgrep；返回 findings + exit_ok（装载是否成功）。

        semgrep 命中规则时正常返回 0；非零退出只代表**套路失败**（规则读取失败、规则自身
        语法错误、联网源则是**拉取失败**）。记 warning 但**不阻断**，仍尽力解析 stdout 里
        已有的结果。二进制缺失（OSError）等同失败，由调用方决定兜底。
        """
        try:
            res = await self.runner.run(
                "semgrep",
                ["--config", config, "--json", "--disable-version-check", "."],
                cwd=root,
            )
        except OSError:
            logger.warning("静态分析：semgrep 未安装，跳过（降级）")
            return _SemgrepRun([], exit_ok=False)
        if res.exit_code != 0:
            detail = res.stderr.strip() or res.stdout.strip()
            logger.warning(
                "静态分析：semgrep 规则源 %s 装载失败（exit=%s，按降级处理）：%s",
                config, res.exit_code, _snippet(detail),
            )
        return _SemgrepRun(_parse_semgrep(res.stdout), exit_ok=res.exit_code == 0)

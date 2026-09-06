"""Agentic 六只只读结构化工具（DESIGN §12.1），无 run_command（壳命令）。

- `grep_repo` / `read_file` / `file_read_diff` / `file_find`：只读探索，返回值 JSON 字符串。
- `code_comment`：LLM **唯一**上报评论的通道（缺 path 回退 groupKey；category/severity 归一化）。
- `task_done`：终止循环（FAILED → 置 failed 标志）。

安全护栏（§12.2）：拒绝对 `..` 的路径穿越（可能逃出只读仓库根）；read_file 每文件 ≤500 行；
grep/file_find 命中超限则截断并提示。全部在 `RepoContext` 的 workspace 内执行。
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from codereview_ai.domain.models import Category, Finding, Severity

logger = logging.getLogger("codereview_ai.agentic.tools")

#: 工具返回给 LLM 的阈值上限（§12.1）。
MAX_READ_LINES = 500
MAX_GREP_HITS = 100
MAX_FIND_RESULTS = 100

_CATEGORIES = {c.value: c for c in Category}
_SEVERITIES = {s.value: s for s in Severity}


@dataclass
class RepoContext:
    """工具执行所在的只读上下文：工作区根 + 已解析 diff + code_comment 的 path 回退键。"""

    workspace: Path  # 仓库只读根（真实=挂载容器 /repo，离线=临时物化目录）
    diff_map: dict[str, str] = field(default_factory=dict)  # path -> unified diff 文本
    group_key: str = ""  # code_comment 缺 path 时的回退 path

    def resolve(self, rel: str) -> Path | None:
        """把相对路径安全解析到工作区内；路径穿越/越界返回 None。"""
        if not rel or rel.startswith(("..", "/")):
            return None
        target = (self.workspace / rel).resolve()
        root = self.workspace.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        return target


@dataclass
class ToolState:
    """跨轮 mutating 状态：code_comment 汇总的 comments 与 task_done 的终止标志。"""

    comments: list[Finding] = field(default_factory=list)
    done: bool = False
    failed: bool = False


class AgentTool(Protocol):
    name: str
    description: str
    params: dict[str, Any]

    def run(self, ctx: RepoContext, state: ToolState, args: dict[str, Any]) -> str: ...


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


# ── 只读探索工具 ────────────────────────────────────────────────────────


def _t_read_file(ctx: RepoContext, _state: ToolState, args: dict[str, Any]) -> str:
    rel = str(args.get("file_path") or "")
    path = ctx.resolve(rel)
    if path is None or not path.is_file():
        return json.dumps({"ok": False, "error": f"文件不存在或越界: {rel}"}, ensure_ascii=False)
    try:
        data = path.read_bytes()
        if _is_binary(data):
            return json.dumps({"ok": False, "error": "二进制文件，跳过"}, ensure_ascii=False)
        text = data.decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    start = max(1, int(args.get("start_line") or 1))
    end = int(args.get("end_line") or start + MAX_READ_LINES - 1)
    if end - start + 1 > MAX_READ_LINES:
        end = start + MAX_READ_LINES - 1  # 每文件 ≤500 行，超出截断
    truncated = end < len(text)
    lines = [(i + 1, text[i]) for i in range(start - 1, min(end, len(text)))]
    payload = {
        "path": rel,
        "truncated": truncated,
        "lines": [f"{n}: {ln}" for n, ln in lines],
    }
    return json.dumps(payload, ensure_ascii=False)


def _t_grep_repo(ctx: RepoContext, _state: ToolState, args: dict[str, Any]) -> str:
    needle = str(args.get("search_text") or "")
    case_sensitive = bool(args.get("case_sensitive", False))
    patterns = args.get("file_patterns") or []
    if not needle:
        return json.dumps({"ok": False, "error": "缺少 search_text"}, ensure_ascii=False)
    hits: list[str] = []
    if not case_sensitive:
        needle = needle.lower()
    for fp in ctx.workspace.rglob("*"):
        if not fp.is_file() or _is_binary(fp.read_bytes()[:4096]):
            continue
        rel = str(fp.relative_to(ctx.workspace)).replace("\\", "/")
        if patterns and not any(fnmatch.fnmatch(rel, p) for p in patterns):
            continue
        for n, ln in enumerate(fp.read_text("utf-8", errors="replace").splitlines(), 1):
            hay = ln if case_sensitive else ln.lower()
            if needle in hay:
                hits.append(f"{rel}:{n}: {ln}")
                if len(hits) >= MAX_GREP_HITS:
                    return json.dumps(
                        {"ok": True, "truncated": True,
                         "hits": hits, "note": f"命中超 {MAX_GREP_HITS}，已截断"},
                        ensure_ascii=False,
                    )
    return json.dumps({"ok": True, "truncated": False, "hits": hits}, ensure_ascii=False)


def _t_file_find(ctx: RepoContext, _state: ToolState, args: dict[str, Any]) -> str:
    query = str(args.get("query_name") or "").lower()
    case_sensitive = bool(args.get("case_sensitive", False))
    if not query:
        return json.dumps({"ok": False, "error": "缺少 query_name"}, ensure_ascii=False)
    q = query if case_sensitive else query.lower()
    found: list[str] = []
    for fp in ctx.workspace.rglob("*"):
        if not fp.is_file():
            continue
        name = fp.name
        if (name if case_sensitive else name.lower()).find(q) != -1:
            found.append(str(fp.relative_to(ctx.workspace)).replace("\\", "/"))
            if len(found) >= MAX_FIND_RESULTS:
                return json.dumps(
                    {"ok": True, "truncated": True, "paths": found,
                     "note": f"结果超 {MAX_FIND_RESULTS}，已截断"},
                    ensure_ascii=False,
                )
    if not found:
        return json.dumps({"ok": True, "paths": [], "note": "not found"}, ensure_ascii=False)
    return json.dumps({"ok": True, "truncated": False, "paths": found}, ensure_ascii=False)


def _t_file_read_diff(ctx: RepoContext, _state: ToolState, args: dict[str, Any]) -> str:
    paths = args.get("path_array") or []
    blocks: list[str] = []
    for p in paths:
        key = str(p)
        diff = ctx.diff_map.get(key) or ctx.diff_map.get(key.removeprefix("/"))
        if diff is None:
            blocks.append(f"==== FILE: {key} ====\n（无此文件的已解析 diff）")
        else:
            blocks.append(f"==== FILE: {key} ====\n{diff}")
    return "\n\n".join(blocks)


# ── 上报/终止工具 ───────────────────────────────────────────────────────


def _norm(path: str) -> str:
    return path.removeprefix("/")


def _t_code_comment(ctx: RepoContext, state: ToolState, args: dict[str, Any]) -> str:
    items = args.get("comments") or []
    if not isinstance(items, list) or not items:
        return json.dumps({"ok": False, "error": "缺少 comments 数组"}, ensure_ascii=False)
    for it in items:
        if not isinstance(it, dict):
            continue
        path = _norm(str(it.get("path") or ctx.group_key or ""))
        content = str(it.get("content") or "").strip()
        if not path or not content:
            continue
        cat = _CATEGORIES.get(str(it.get("category")), Category.OTHER)
        sev = _SEVERITIES.get(str(it.get("severity")), Severity.LOW)
        state.comments.append(Finding(
            content=content,
            category=cat,
            severity=sev,
            existing_code=str(it.get("existing_code") or ""),
            suggestion_code=str(it.get("suggestion_code")) or None,
            file=path,
            side="RIGHT",
            source="agent",
        ))
    return "Successfully commented."


def _t_task_done(ctx: RepoContext, state: ToolState, args: dict[str, Any]) -> str:
    st = str(args.get("state") or "DONE").upper()
    if st == "FAILED":
        state.failed = True
        state.done = True
        return "FAILED"
    state.done = True
    return "DONE"


# ── 注册表 ──────────────────────────────────────────────────────────────


_TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "code_comment": (
        "上报一条评论；LLM 产出审查意见的唯一通道",
        {"comments": "list[{existing_code,suggestion_code,category,severity,path,thinking}]"},
        _t_code_comment,
    ),
    "grep_repo": (
        "在仓库内搜索文本，命中超 100 截断",
        {"search_text": "str", "case_sensitive": "bool", "file_patterns": "list[str]"},
        _t_grep_repo,
    ),
    "read_file": (
        "读文件指定行区间（每文件 ≤500 行）",
        {"file_path": "str", "start_line": "int", "end_line": "int"},
        _t_read_file,
    ),
    "file_read_diff": ("按路径返回已解析的 diff", {"path_array": "list[str]"}, _t_file_read_diff),
    "file_find": (
        "按文件名查找路径（≤100）",
        {"query_name": "str", "case_sensitive": "bool"},
        _t_file_find,
    ),
    "task_done": ("声明审查结束，state∈DONE|FAILED", {"state": "str"}, _t_task_done),
}


class ToolRunner:
    """按名执行工具；未知工具返回错误串（LLM 可感知），不抛中断整轮。"""

    def __init__(self, ctx: RepoContext, state: ToolState) -> None:
        self._ctx = ctx
        self._state = state

    @property
    def state(self) -> ToolState:
        return self._state

    def names(self) -> list[str]:
        return list(_TOOLS)

    def run_one(self, name: str, args: dict[str, Any]) -> str:
        spec = _TOOLS.get(name)
        if spec is None:
            return json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False)
        _desc, _params, impl = spec
        try:
            out = impl(self._ctx, self._state, args if isinstance(args, dict) else {})
        except Exception as exc:  # noqa: BLE001 —— 单个工具失败不中断会话
            logger.warning("agentic 工具 %s 执行异常：%s", name, exc)
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        return str(out)


def tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """导出给 LLM 的 OpenAI 风格工具描述（含参数 schema）。"""
    out: list[dict[str, Any]] = []
    for name, (_desc, params, _impl) in _TOOLS.items():
        if names and name not in names:
            continue
        props = {k: {"type": "string"} for k in params}
        if any(v == "int" for v in params.values()):
            props = {k: ({"type": "integer"} if v == "int" else {"type": "string"})
                     for k, v in params.items()}
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": _desc,
                "parameters": {"type": "object", "properties": props},
            },
        })
    return out

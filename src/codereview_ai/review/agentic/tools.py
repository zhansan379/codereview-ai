"""Agentic 六只只读结构化工具（DESIGN §12.1），无 run_command（壳命令）。

- `grep_repo` / `read_file` / `file_read_diff` / `file_find`：只读探索，返回值 JSON 字符串。
- `code_comment`：LLM **唯一**上报评论的通道（缺 path 回退 groupKey；category/severity 归一化）。
- `task_done`：终止循环（FAILED → 置 failed 标志）。

安全护栏（§12.2）：拒绝对 `..` 的路径穿越（可能逃出只读仓库根）；read_file 每文件 ≤500 行；
grep/file_find 命中超限则截断并提示。真实运行（有 `repo_dir`+`pinned_sha`）改读**不可变
git 对象**，免疫并发审查下工作树被其它 PR `reset` 覆盖的竞态；离线/测试回退工作区路径读。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import subprocess
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
# 导出给 schema 的枚举可选项（§12.1 code_comment 标准嵌套结构）。
_CATEGORY_ENUM = [c.value for c in Category]
_SEVERITY_ENUM = [s.value for s in Severity]


def _safe_rel(rel: str) -> str | None:
    """把相对路径归一成 git 对象地址用的安全 key；穿越/绝对/杂项返回 None。"""
    if not rel or rel.startswith(("..", "/", "\\")):
        return None
    if "\\" in rel or any(p == ".." for p in rel.split("/")):
        return None
    return rel


def _git(repo_dir: Path, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    """在 `repo_dir`(git 仓库根) 跑只读 git 子命令，失败返回 None（工具层不抛）。"""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args], check=False, capture_output=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


@dataclass
class RepoContext:
    """工具执行所在的只读上下文：工作区根 + 已解析 diff + code_comment 的 path 回退键。

    真实运行（`LocalCloneRuntime`）会注入 `repo_dir` + `pinned_sha`，三个读工作区的工具
    （read_file/grep_repo/file_find）改走**不可变 git 对象**（按 `pinned_sha` 寻址），
    从而免疫并发审查下工作树被其它 PR `reset` 覆盖的竞态——git 对象按 sha 只读不变，
    而工作树可变。离线/测试（`FakeRuntime`）不给 sha，自然回退到 `workspace` 路径读。
    """

    workspace: Path  # 仓库只读根（真实=挂载容器 /repo，离线=临时物化目录）
    diff_map: dict[str, str] = field(default_factory=dict)  # path -> unified diff 文本
    group_key: str = ""  # code_comment 缺 path 时的回退 path
    repo_dir: Path | None = None  # git 仓库根（含 .git）
    pinned_sha: str | None = None  # 不可变读源：读取一律按此 sha 寻址 git 对象

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

    # ── 不可变 git 对象只读源（有 repo_dir+pinned_sha 时启用）────────────
    def repo_git(self) -> bool:
        """是否走 git 对象只读（而非可变工作树路径）。"""
        return bool(self.repo_dir and self.pinned_sha)

    def git_blob(self, rel: str) -> bytes | None:
        """按 `pinned_sha` 读不可变 blob；路径非法/不存在/读取失败返回 None。"""
        key = _safe_rel(rel)
        if key is None:
            return None
        proc = _git(self.repo_dir, ["cat-file", "blob", f"{self.pinned_sha}:{key}"])
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout

    def git_paths(self) -> list[str]:
        """`pinned_sha` 树上全部文件路径（git 恒用 `/` 分隔）。"""
        proc = _git(self.repo_dir, ["ls-tree", "-r", "--name-only", str(self.pinned_sha)])
        if proc is None or proc.returncode != 0:
            return []
        return [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln]

    def git_grep(self, needle: str, *, case_sensitive: bool) -> list[tuple[str, int, str]]:
        """`git grep` 在 `pinned_sha` 树上搜固定子串，返回 (path, lineno, content)。"""
        args = ["grep", "-F", "-n"]
        if not case_sensitive:
            args.append("-i")
        args += ["-e", needle, str(self.pinned_sha)]
        proc = _git(self.repo_dir, args)
        if proc is None or proc.returncode not in (0, 1):  # 1 = 无命中（git 约定）
            return []
        hits: list[tuple[str, int, str]] = []
        # 在树上 grep（给定了 rev）时每行带 `<rev>:<path>:<line>:<content>` 前缀；剥掉 rev 段。
        prefix = f"{self.pinned_sha}:"
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            line = line[len(prefix):] if line.startswith(prefix) else line
            path, sep, rest = line.partition(":")
            if not sep:
                continue
            ln_s, sep2, content = rest.partition(":")
            if not sep2:
                continue
            try:
                ln = int(ln_s)
            except (TypeError, ValueError):
                continue
            hits.append((path, ln, content))
        return hits


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
    if ctx.repo_git():
        data = ctx.git_blob(rel)  # 不可变对象读，工作树被并发 reset 不受影响
    else:
        path = ctx.resolve(rel)
        try:
            data = path.read_bytes() if (path is not None and path.is_file()) else None
        except OSError:
            data = None
    if data is None:
        return json.dumps({"ok": False, "error": f"文件不存在或越界: {rel}"}, ensure_ascii=False)
    if _is_binary(data):
        return json.dumps({"ok": False, "error": "二进制文件，跳过"}, ensure_ascii=False)
    text = data.decode("utf-8", errors="replace").splitlines()

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
    if ctx.repo_git():
        # 不可变对象读：git grep 在 pinned_sha 树上搜，主体跨文件刷新安全
        for path, ln, content in ctx.git_grep(needle, case_sensitive=case_sensitive):
            if patterns and not any(fnmatch.fnmatch(path, p) for p in patterns):
                continue
            hits.append(f"{path}:{ln}: {content}")
            if len(hits) >= MAX_GREP_HITS:
                return json.dumps(
                    {"ok": True, "truncated": True,
                     "hits": hits, "note": f"命中超 {MAX_GREP_HITS}，已截断"},
                    ensure_ascii=False,
                )
        return json.dumps({"ok": True, "truncated": False, "hits": hits}, ensure_ascii=False)

    # 路径模式（离线/测试）：直接遍历工作区
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
    paths = ctx.git_paths() if ctx.repo_git() else [
        str(p.relative_to(ctx.workspace)).replace("\\", "/")
        for p in ctx.workspace.rglob("*") if p.is_file()
    ]
    for path in paths:
        name = Path(path).name
        if (name if case_sensitive else name.lower()).find(q) != -1:
            found.append(path)
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


#: 评论正文归一化：去空白折叠 + 小写，用于内容指纹去重（防同问题多组/多轮重复上报）。
_WS_RE = re.compile(r"\s+")


def finding_fingerprint(f: Finding) -> tuple[str, ...]:
    """内容指纹（PR-Agent `body_fp OR code_fp`）：正文与 existing_code 归一化后的指纹集。

    两个 fingerprint（body/code）都是归一化串；任一命中即视为重复，因此返回去重后的有序元组。
    """
    def _fp(text: str) -> str:
        return _WS_RE.sub(" ", text.strip().lower())

    fps = {x for x in (_fp(f.content), _fp(f.existing_code)) if x}
    return tuple(sorted(fps))


def finding_matches(a: Finding, b: Finding) -> bool:
    """同文件且指纹有交集即视为重复评论（跨分组/跨轮兜底去重）。"""
    if a.file != b.file:
        return False
    a_fps, b_fps = finding_fingerprint(a), finding_fingerprint(b)
    if not a_fps or not b_fps:
        return False
    return bool(set(a_fps) & set(b_fps))


def _as_int(v: Any) -> int | None:
    """把上报的 line 归一成 int；缺省/非法返回 None（回落总结评论）。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _t_code_comment(ctx: RepoContext, state: ToolState, args: dict[str, Any]) -> str:
    items = args.get("comments") or []
    if not isinstance(items, list) or not items:
        logger.warning("code_comment: comments 非数组（%s→%r），args keys=%s",
                       type(items).__name__, type(items), list(args.keys()))
        return json.dumps({"ok": False, "error": "缺少 comments 数组"}, ensure_ascii=False)
    dropped = 0
    accepted = 0
    dropped_keys: dict[str, int] = {}
    sample_shown = False
    for it in items:
        if not isinstance(it, dict):
            dropped += 1
            if not sample_shown:
                sample_shown = True
                logger.warning("code_comment 丢弃非对象项 %r", it)
            continue
        path = _norm(str(it.get("path") or ctx.group_key or ""))
        content = str(it.get("content") or "").strip()
        if not path or not content:
            # 记下被丢条目的字段结构 + 缺哪个字段，诊断"模型交了但没落库"（DeepSeek 常把
            # 正文塞进 content 之外的键，或 content 为空串被 strip 掉）。始终打印一整个条目
            # 的原始 keys，好确认模型实际发的字段名，而不是只猜缺 content/path。
            dropped += 1
            for k in ("path", "content"):
                if not it.get(k):
                    dropped_keys[k] = dropped_keys.get(k, 0) + 1
            if not sample_shown:
                sample_shown = True
                logger.warning("code_comment 丢弃条目样例 keys=%s values=%r",
                               list(it.keys()), {k: (v[:60] if isinstance(v, str) else v)
                                                 for k, v in it.items()})
            continue
        cat = _CATEGORIES.get(str(it.get("category")), Category.OTHER)
        sev = _SEVERITIES.get(str(it.get("severity")), Severity.LOW)
        line = _as_int(it.get("line"))
        old_line = _as_int(it.get("old_line"))
        # agent 在 clone 全仓（reset 到 head）里读到的行号 = MR head 侧行号 = diff 新侧行号，
        # 天然对齐；line 落在 diff hunk 可评论范围内时由 ResultWriter 发行级 inline，
        # 越界/缺省则回落总结评论（result_writer.partition_findings）。
        state.comments.append(Finding(
            content=content,
            category=cat,
            severity=sev,
            file=path,
            side="RIGHT",
            existing_code=str(it.get("existing_code") or ""),
            suggestion_code=str(it.get("suggestion_code")) or None,
            line=line,
            old_line=old_line,
            source="agent",
        ))
        accepted += 1
    if dropped:
        logger.warning(
            "code_comment: 本条 %d 条中丢弃 %d（缺 path/content %s）、accept %d",
            len(items), dropped, dict(dropped_keys), accepted,
        )
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


# code_comment 的 comments 元素：给 LLM 的标准嵌套 object schema。此前把它建模成
# 一行散文字符串（type:"string"），模型拿不到真正的字段类型/必填/枚举，只能靠蒙，
# 导致正文偶尔塞进 content 外的键、数组塞成字符串时被 _t_code_comment 整条丢弃。
# 改为标准 `array → items(object)` 后，模型照说明书填，丢弃率显著下降。
_COMMENT_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string",
                 "description": "相对仓库根的文件路径，如 src/main.py（必填）"},
        "content": {"type": "string",
                    "description": "批注正文：点明问题与建议（必填）"},
        "existing_code": {"type": "string",
                          "description": "定位锚点：问题处的现有代码片段，无 line 时用"},
        "suggestion_code": {"type": "string",
                            "description": "可选：建议替换成的新代码"},
        "category": {"type": "string", "enum": _CATEGORY_ENUM,
                     "description": "问题类别，取枚举之一"},
        "severity": {"type": "string", "enum": _SEVERITY_ENUM,
                     "description": "严重度，取枚举之一"},
        "line": {"type": "integer",
                 "description": "问题所在文件的新侧行号（参考 read_file 结果）"},
        "old_line": {"type": "integer"},
    },
    "required": ["path", "content"],
}

_TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "code_comment": (
        "上报审查意见；LLM 产出结论的唯一通道。一次传一条或多条，每条是带 path/content "
        "的对象。line 为该问题在文件中的新侧行号（可从 read_file 的结果得知；填了且落在 "
        "diff 范围内会定位到具体代码行，不填则并入总结评论）",
        {"comments": {"type": "array", "items": _COMMENT_ITEM,
                      "description": "本次要上报的意见列表"}},
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
        props: dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, dict) and "type" in v:
                props[k] = v  # 已是标准 JSON-schema 属性（如 code_comment 的嵌套 items），透传
            else:
                props[k] = {"type": "integer" if v == "int" else "string"}
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": _desc,
                "parameters": {"type": "object", "properties": props},
            },
        })
    return out

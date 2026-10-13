"""评审文件默认范围过滤（对齐 OCR `internal/config/allowlist/`）。

OCR 的 review 命令默认只审「扩展名白名单内的代码文件」并排除「测试/快照/fixtures 等
非产品代码路径」；本模块把这两份默认清单原样移植过来，让本项目的评审范围与 OCR 一致：

- `DEFAULT_ALLOWED_EXTENSIONS` ← OCR `supported_file_types.json`（无扩展名文件放行，
  如 Makefile/Dockerfile——对齐 OCR `whyExcluded`：`ext != ""` 才检查白名单）。
- `DEFAULT_EXCLUDE_GLOBS` ← OCR `default_exclude_patterns.json`（doublestar 语义的
  glob：`**` 跨目录、`*` 不跨、`{a,b}` 择一；大小写不敏感）。

在评审入口（agentic 路径）应用 `filter_review_scope`，避免把测试文件/生成物喂给
模型产生大量低价值评论——这是 OCR 默认行为，也是两边指标可比的前提。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from codereview_ai.domain.models import FileDiff

#: OCR supported_file_types.json 全量（小写，含前导点）
DEFAULT_ALLOWED_EXTENSIONS = frozenset({
    ".java", ".kt", ".kts", ".scala", ".groovy", ".py", ".pyi", ".js", ".jsx",
    ".ts", ".tsx", ".mjs", ".cjs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".hxx", ".cs", ".vb", ".fs", ".go", ".rs", ".rb", ".rake", ".gemspec",
    ".php", ".phtml", ".swift", ".m", ".mm", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".sql", ".css", ".scss", ".sass", ".less", ".html", ".htm", ".ftl",
    ".ftlh", ".ftlx", ".hbs", ".mustache", ".pug", ".astro", ".vue", ".ipynb",
    ".svelte", ".xml", ".yaml", ".yml", ".json", ".toml", ".ini", ".env",
    ".gradle", ".cmake", ".r", ".lua", ".pl", ".pm", ".ex", ".exs", ".erl",
    ".hrl", ".ets", ".json5", ".dart", ".tf", ".graphql", ".gql", ".prisma",
    ".jl", ".hcl", ".tfvars", ".bicep", ".proto", ".nix", ".hs", ".lhs", ".nim",
    ".nims", ".nimble", ".elm", ".properties", ".po", ".pot", ".jsonnet",
    ".libsonnet", ".zig", ".thrift", ".capnp", ".v", ".sv", ".vh", ".vhd",
    ".vhdl", ".sol", ".vy",
})

#: OCR default_exclude_patterns.json 全量（doublestar glob，大小写不敏感）
DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = (
    "**/*_test.go",
    "**/src/test/java/**/*.java",
    "**/src/test/**/*.{kt,kts}",
    "**/*.test.{js,jsx,ts,tsx}",
    "**/*.spec.{js,jsx,ts,tsx}",
    "**/__tests__/**",
    "**/test/**/*_test.py",
    "**/tests/**/*_test.py",
    "**/*_test.py",
    "**/*_spec.rb",
    "**/spec/**/*_spec.rb",
    "**/*Test.java",
    "**/*Tests.java",
    "**/*_test.rs",
    "**/oh_modules/**",
    "**/*.test.ets",
    "**/test/**/*.jl",
    "**/test/**/*.hs",
    "**/*Spec.hs",
    "**/test/**/*.lhs",
    "**/*Spec.lhs",
    "**/tests/**/*.nim",
    "**/tests/**/*.R",
    "**/__snapshots__/**",
    "**/*.snap",
    "**/testdata/**",
    "**/fixtures/**",
    "**/.ipynb_checkpoints/**",
    "**/*.generated.*",
    "**/*.gen.go",
    "**/*.pb.go",
    "**/*.pb.cc",
    "**/*.pb.h",
    "**/*Test.swift",
    "**/*Tests.swift",
    "**/Tests/**/*.swift",
    "**/tests/**/*.elm",
    "**/vendor/**/*.{jsonnet,libsonnet}",
    "**/test/**/*.zig",
    "**/*_test.zig",
    "**/kitex_gen/**/*.go",
    "**/*.capnp.h",
    "**/*.capnp.go",
    "**/*.capnp.ts",
    "**/*_capnp.rs",
    "**/*_capnp.py",
    "**/tb_*.{v,sv,vhd,vhdl}",
    "**/*_tb.{v,sv,vhd,vhdl}",
    "lib/**/*.sol",
    "**/*.t.sol",
    "**/test/**/*.sol",
    "**/tests/**/*.sol",
    "**/test/**/*.vy",
    "**/tests/**/*.vy",
)


def _translate_glob(glob: str) -> str:
    """glob → 正则（doublestar 语义）：`**/` 跨零或多层目录、`*` 段内、`{a,b}` 择一。"""
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:[^/]+/)*")  # `**/` 含零层
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "{":
            j = glob.index("}", i)
            out.append("(?:" + "|".join(re.escape(a) for a in glob[i + 1:j].split(",")) + ")")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(_translate_glob(pattern), re.IGNORECASE)


def _norm_path(path: str) -> str:
    """归一化：反斜杠 → 正斜杠、去前导 './'（glob 按仓库相对路径匹配）。"""
    p = (path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_excluded_path(path: str, patterns: Iterable[str] = DEFAULT_EXCLUDE_GLOBS) -> bool:
    """路径命中任一默认排除 glob（测试/快照/fixtures/生成物等）→ True。"""
    p = _norm_path(path)
    return any(_compiled(pat).match(p) for pat in patterns)


def _extension(path: str) -> str:
    """取小写扩展名（含点）；无扩展名返回 ""（对齐 Go `filepath.Ext`：末元素末点之后）。"""
    base = _norm_path(path).rsplit("/", 1)[-1]
    idx = base.rfind(".")
    return base[idx:].lower() if idx >= 0 else ""


def is_allowed_extension(
    path: str, allowed: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS
) -> bool:
    """扩展名在白名单内 → True；无扩展名文件放行（对齐 OCR：仅 `ext != ""` 才过滤）。"""
    ext = _extension(path)
    return ext == "" or ext in allowed


def filter_review_scope(
    diffs: Sequence[FileDiff],
    *,
    allowed_extensions: frozenset[str] | None = DEFAULT_ALLOWED_EXTENSIONS,
    exclude_globs: Iterable[str] = DEFAULT_EXCLUDE_GLOBS,
) -> tuple[list[FileDiff], list[str]]:
    """默认范围过滤：返回 (保留的 diffs, 跳过路径列表)。

    判据（与 OCR `whyExcluded` 同序）：扩展名不在白名单 → 跳过；命中排除 glob → 跳过。
    `allowed_extensions=None` 时不查扩展名（项目自定义 file_extensions 已先行过滤的场景）。
    纯删除文件（new_path 缺省/`/dev/null`）不做扩展名判断，仅查排除 glob 的旧路径。
    """
    kept: list[FileDiff] = []
    skipped: list[str] = []
    for d in diffs:
        path = d.new_path if d.new_path and d.new_path != "/dev/null" else d.old_path
        if not path or path == "/dev/null":
            kept.append(d)
            continue
        if allowed_extensions is not None and not is_allowed_extension(path, allowed_extensions):
            skipped.append(path)
            continue
        if is_excluded_path(path, exclude_globs):
            skipped.append(path)
            continue
        kept.append(d)
    return kept, skipped

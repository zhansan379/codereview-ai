"""`codereview_ai.review.file_scope` 单测：默认审查范围（对齐 OCR allowlist 两份清单）。

- 扩展名白名单：命中放行；`.md` 等非白名单扩展跳过；无扩展名文件（Makefile）放行。
- 排除 glob：OCR default_exclude_patterns.json 的 doublestar 语义（`**/` 跨零多层、
  `{a,b}` 择一、大小写不敏感）——测试/快照/fixtures/生成物命中即跳过。
- `filter_review_scope`：keep/skip 分流、纯删除文件按旧路径判、白名单可关闭。
"""

from __future__ import annotations

from codereview_ai.domain.models import ChangeType, FileDiff
from codereview_ai.review.file_scope import (
    DEFAULT_ALLOWED_EXTENSIONS,
    DEFAULT_EXCLUDE_GLOBS,
    filter_review_scope,
    is_allowed_extension,
    is_excluded_path,
)


def _fd(path: str, old_path: str | None = None) -> FileDiff:
    old = old_path if old_path is not None else path
    return FileDiff(old, path, "+ x\n", 1, 0, ChangeType.MODIFIED, "x\n")


# ── 扩展名白名单 ────────────────────────────────────────────────────────


def test_extension_whitelist_ocr_semantics() -> None:
    assert is_allowed_extension("server/create.go")
    assert is_allowed_extension("src/main.py")
    # 无扩展名文件放行（OCR：ext == "" 不查白名单，Makefile/Dockerfile 可审）
    assert is_allowed_extension("Makefile")
    assert is_allowed_extension("scripts/deploy")
    # .md 不在 OCR supported_file_types.json 内 → 跳过
    assert not is_allowed_extension("README.md")
    assert not is_allowed_extension("docs/guide.rst")
    # 末元素末点后取扩展名（= Go filepath.Ext）：foo.d.ts → .ts 放行
    assert is_allowed_extension("src/foo.d.ts")


def test_allowed_extensions_default_matches_ocr_list_size() -> None:
    # supported_file_types.json 为 106 项；防手滑删项
    assert len(DEFAULT_ALLOWED_EXTENSIONS) == 106
    assert ".go" in DEFAULT_ALLOWED_EXTENSIONS and ".py" in DEFAULT_ALLOWED_EXTENSIONS
    assert ".md" not in DEFAULT_ALLOWED_EXTENSIONS


# ── 排除 glob（doublestar 语义） ────────────────────────────────────────


def test_exclude_globs_tests_generated_fixtures() -> None:
    assert is_excluded_path("server/create_test.go")
    assert is_excluded_path("create_test.go")  # **/ 跨零层目录
    assert is_excluded_path("pkg/a/b/__tests__/x.tsx")
    assert is_excluded_path("benchmarks/foo.snap")
    assert is_excluded_path("testdata/in.json")
    assert is_excluded_path("internal/fixtures/data.go")
    assert is_excluded_path("api/thing.pb.go")
    assert is_excluded_path("gen/model.generated.ts")
    assert not is_excluded_path("server/create.go")
    assert not is_excluded_path("tests_helper/readme.py")  # 非 glob 命中路径


def test_exclude_globs_braces_and_case_insensitive() -> None:
    assert is_excluded_path("src/a.test.ts")
    assert is_excluded_path("src/a.test.tsx")
    assert is_excluded_path("pkg/FooTest.java")  # 大小写不敏感（*Test.java）
    assert not is_excluded_path("src/atest.ts")


# ── filter_review_scope 分流 ────────────────────────────────────────────


def test_filter_review_scope_keep_and_skip() -> None:
    diffs = [
        _fd("server/create.go"),
        _fd("server/create_test.go"),
        _fd("README.md"),
        _fd("docs/rare.xml"),
    ]
    kept, skipped = filter_review_scope(diffs)
    assert [d.new_path for d in kept] == ["server/create.go", "docs/rare.xml"]
    assert skipped == ["server/create_test.go", "README.md"]


def test_filter_review_scope_deleted_file_uses_old_path() -> None:
    # 纯删除：new_path=/dev/null → 按旧路径判：删掉的 test 文件照样跳过
    deleted_test = FileDiff("server/create_test.go", "/dev/null", "- x\n", 0, 1,
                            ChangeType.DELETED_FILE, "")
    deleted_src = FileDiff("server/create.go", "/dev/null", "- x\n", 0, 1,
                           ChangeType.DELETED_FILE, "")
    kept, skipped = filter_review_scope([deleted_test, deleted_src])
    assert [d.old_path for d in kept] == ["server/create.go"]
    assert skipped == ["server/create_test.go"]


def test_filter_review_scope_can_disable_extension_check() -> None:
    # 项目自定义 file_extensions 已先行过滤的场景：关闭扩展名白名单，只留排除 glob
    diffs = [_fd("README.md"), _fd("server/create_test.go")]
    kept, skipped = filter_review_scope(diffs, allowed_extensions=None)
    assert [d.new_path for d in kept] == ["README.md"]
    assert skipped == ["server/create_test.go"]


def test_exclude_glob_count_matches_ocr_defaults() -> None:
    # default_exclude_patterns.json 为 54 项（逐条与 OCR 原文件比对过，防手滑删项）
    assert len(DEFAULT_EXCLUDE_GLOBS) == 54

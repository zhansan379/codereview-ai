"""离线单测：未变更文件复用（reuse 纯函数）。

- `file_content_key`：sha1(new_file_content)；无新侧内容（删除/纯旧侧）→ None（必须保留）。
- `covered_file_map`：`{new_path 归一: sha1}`，仅收有新侧内容的文件。
- `prune_unchanged`：增量轮按内容哈希剪掉未变文件；新文件/变文件/删除保留；无覆盖集全保留。
"""

from __future__ import annotations

from codereview_ai.domain.models import ChangeType, FileDiff
from codereview_ai.review.reuse import covered_file_map, file_content_key, prune_unchanged


def _fd(path: str, content: str = "x = 1\n", diff: str | None = None,
        *, deleted: bool = False) -> FileDiff:
    if deleted:
        return FileDiff(path, path, "- old\n", 0, 1, ChangeType.DELETED_FILE, "")
    diff = diff if diff is not None else f"+ {content}"
    return FileDiff(path, path, diff, 1, 0, ChangeType.MODIFIED, content)


# ── 内容哈希 ───────────────────────────────────────────────────────────


def test_file_content_key_none_for_deleted():
    assert file_content_key(_fd("a.py", deleted=True)) is None
    assert file_content_key(_fd("a.py")) is not None


def test_covered_file_map_strips_leading_slash():
    d = FileDiff("/pkg/a.py", "/pkg/a.py", "+ x\n", 1, 0, ChangeType.MODIFIED, "x\n")
    m = covered_file_map([d])
    assert list(m) == ["pkg/a.py"]  # removeprefix("/")
    assert m["pkg/a.py"] == file_content_key(d)


def test_covered_file_map_skips_no_new_content():
    d = _fd("a.py", deleted=True)
    assert covered_file_map([d]) == {}


# ── 裁剪 ───────────────────────────────────────────────────────────────


def test_prune_unchanged_keeps_only_changed_or_new():
    last = covered_file_map([_fd("a.py", "v1\n")])
    kept = prune_unchanged([_fd("a.py", "v1\n"), _fd("c.py", "v3\n")], last)
    # a.py 内容未变 → 剪掉；c.py 新文件 → 保留
    assert [d.new_path for d in kept] == ["c.py"]


def test_prune_unchanged_keeps_changed_content():
    last = covered_file_map([_fd("a.py", "v1\n")])
    changed = _fd("a.py", "v2\n")
    assert [d.new_path for d in prune_unchanged([changed], last)] == ["a.py"]


def test_prune_unchanged_no_covered_returns_all():
    diffs = [_fd("a.py"), _fd("b.py")]
    assert prune_unchanged(diffs, None) is not diffs  # 拷贝，不引用泄露
    assert len(prune_unchanged(diffs, None)) == 2
    assert len(prune_unchanged(diffs, {})) == 2


def test_prune_unchanged_keeps_deleted_file():
    # 删除/新增无新侧内容 → h=None → 永远保留（复用不吞掉被删文件）
    d = _fd("a.py", deleted=True)
    last = {"a.py": "whateverhash"}
    assert [x.new_path for x in prune_unchanged([d], last)] == ["a.py"]


def test_prune_unchanged_can_return_empty():
    last = covered_file_map([_fd("a.py", "v1\n")])
    assert prune_unchanged([_fd("a.py", "v1\n")], last) == []  # 全未变 → 裁空（worker 标 completed-empty）  # noqa: E501

"""`codereview_ai.bench_review` 纯函数单测（AACR-Bench 无头评审入口）。

两种测试，均离线：
- 序列化路径：`_finding_to_dict` / `findings_envelope` / `_cmd_offline` → 合法 JSON envelope。
- 纯本地 git：有 `git` 时在临时仓库构造 base..head 两次提交，验证 `build_file_diffs`
  切块/行号/新侧全文还原正确（无 git 则跳过）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from codereview_ai.bench_review import (
    _cmd_offline,
    _finding_to_dict,
    build_file_diffs,
    findings_envelope,
)
from codereview_ai.domain.models import Category, Finding, Severity


def _make_finding() -> Finding:
    return Finding(
        content="资源未释放。",
        category=Category.BUG,
        severity=Severity.MEDIUM,
        existing_code="f = open(path)",
        file="src/main.py",
        line=4,
        old_line=3,
        side="RIGHT",
        source="agent",
    )


def test_finding_to_dict_normalizes_side_line() -> None:
    d = _finding_to_dict(_make_finding())
    assert d["file"] == "src/main.py"
    assert d["side"] == "right"  # 大写 → 小写
    assert d["line"] == 4  # RIGHT 用新侧行号
    assert d["category"] == "bug"
    assert d["severity"] == "medium"
    assert d["source"] == "agent"


def test_finding_to_dict_left_uses_old_line() -> None:
    f = _make_finding()
    f.side = "LEFT"
    f.line, f.old_line = 5, 2
    d = _finding_to_dict(f)
    assert d["side"] == "left"
    assert d["line"] == 2  # LEFT 用旧侧行号


def test_findings_envelope_shape() -> None:
    env = findings_envelope([_make_finding()], {"input_tokens": 10, "output_tokens": 3})
    assert env["findings"][0]["line"] == 4
    assert env["token_usage"] == {"input_tokens": 10, "output_tokens": 3}


def test_cmd_offline_emits_valid_ok_json(capfd) -> None:  # noqa: ANN001
    rc = _cmd_offline()
    out = capfd.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["error"] == ""
    assert payload["findings"] and payload["findings"][0]["file"] == "demo.py"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip()


def _write(repo: Path, path: str, content: str) -> None:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_build_file_diffs(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        import pytest

        pytest.skip("git 不存在，跳过纯本地 diff 构建测试")
    # 构造 base..head：新增一个文件 + 修改一个文件 + 删除一个文件
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _write(repo, "app.py", "x = 1\n")
    _write(repo, "gone.py", "old\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _write(repo, "app.py", "x = 1\ny = 2  # added\n")
    _write(repo, "new.py", "def f():\n    return 1\n")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")

    diffs = build_file_diffs(repo, base, head)
    by_new = {d.new_path: d for d in diffs}
    assert set(by_new) == {"app.py", "gone.py", "new.py"}

    app = by_new["app.py"]
    assert app.change_type.value == "modified"
    assert app.additions == 1 and app.deletions == 0
    # 产品 new_file_content_from_patch 逐行铺回、不保留尾部换行
    assert app.new_file_content == "x = 1\ny = 2  # added"

    new = by_new["new.py"]
    assert new.change_type.value == "new"
    assert new.new_file_content == "def f():\n    return 1"

    gone = by_new["gone.py"]
    assert gone.change_type.value == "deleted"
    assert gone.new_file_content == ""

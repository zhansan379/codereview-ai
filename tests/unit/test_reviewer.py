"""review/reviewer 测试：文件过滤 + prompt 组装 + 端到端(fake LLM → 锚定定位)。"""

from __future__ import annotations

import asyncio
import json

from codereview_ai.domain.models import Category, ChangeType, FileDiff, PullRequest, Severity
from codereview_ai.review.llm_gateway import LLMGateway
from codereview_ai.review.reviewer import (
    Reviewer,
    ReviewerConfig,
    build_messages,
    filter_files,
)

APP_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def greet(name):\n"
    "     return f\"hi {name}\"\n"
    "+\n"
    "+def live():\n"
    "+    return 1\n"
)
APP_CONTENT = "def greet(name):\n    return f\"hi {name}\"\n\ndef live():\n    return 1\n"

BIG_DIFF = (  # noqa: E501
    "diff --git a/big.py b/big.py\n+++ b/big.py\n@@ -1,1 +1,700 @@\n"
    + "\n".join(f"+line{i}" for i in range(700))
    + "\n"
)


def _pr() -> PullRequest:
    return PullRequest(
        provider="gitlab",
        repo_id="1",
        repo_full_name="acme/app",
        web_url="https://gitlab/acme/app/-/merge_requests/5",
        pr_number=5,
        title="add live",
        source_branch="feat/live",
        target_branch="main",
        head_sha="a" * 40,
        base_sha="b" * 40,
    )


def _diffs() -> list[FileDiff]:
    return [
        FileDiff("app.py", "app.py", APP_DIFF, 3, 0, ChangeType.MODIFIED, APP_CONTENT),
        FileDiff("node_modules/x.js", "node_modules/x.js", "x", 1, 0, ChangeType.MODIFIED),
        FileDiff("big.py", "big.py", BIG_DIFF, 700, 0, ChangeType.MODIFIED),
        FileDiff("gone.py", "/dev/null", "old", 0, 5, ChangeType.DELETED_FILE),
    ]


def test_filter_files_keeps_code_drops_excluded_and_oversize():
    kept, skipped = filter_files(_diffs(), ReviewerConfig())
    new_paths = [d.new_path for d in kept]
    assert "app.py" in new_paths
    assert "node_modules/x.js" not in new_paths  # 忽略路径
    assert "big.py" not in new_paths  # 超行数上限
    # 纯删除文件保留（其 new_path 是 /dev/null，按 old_path 识别）
    assert any(d.new_path == "/dev/null" and d.old_path == "gone.py" for d in kept)
    assert "node_modules/x.js" in skipped
    assert "big.py" in skipped


def test_filter_files_extension_whitelist():
    diffs = _diffs()
    kept, _ = filter_files(diffs, ReviewerConfig(extensions=frozenset({"py", "ts"})))
    assert "node_modules/x.js" not in [d.new_path for d in kept]


def test_build_messages_contains_schema_and_branches():
    msgs = build_messages(
        pr=_pr(),
        commits_text="fix bug",
        diffs=[_diffs()[0]],
        skipped_files=["big.py"],
        cfg=ReviewerConfig(),
    )
    assert msgs[0]["role"] == "system"
    assert "skipped_files" in msgs[0]["content"]
    assert "existing_code" in msgs[0]["content"]
    assert "目标分支：main" in msgs[1]["content"]
    assert "big.py" in msgs[1]["content"]


def _fake_reviewer(backend) -> Reviewer:
    return Reviewer(LLMGateway(model="fake/model", backend=backend))


def test_review_end_to_end_anchors_finding():
    wanted = {
        "summary": "总体良好，有一处需注意。",
        "scores": {
            "correctness": 32, "security": 25, "practices": 16,
            "performance": 4, "commit_quality": 4,
        },
        "findings": [
            {
                "content": "这里可能有边界问题",
                "category": "bug",
                "severity": "high",
                "file": "app.py",
                "existing_code": "def live():",
                "suggestion_code": "def live(p=None):",
            }
        ],
        "skipped_files": ["big.py"],
    }

    async def backend(messages):
        assert messages[0]["role"] == "system"
        return json.dumps(wanted, ensure_ascii=False)

    r = _fake_reviewer(backend)

    async def run():
        return await r.review(pr=_pr(), commits_text="feat", diffs=_diffs())

    result = asyncio.run(run())
    assert result.summary == "总体良好，有一处需注意。"
    assert result.scores.total == 32 + 25 + 16 + 4 + 4
    assert result.skipped_files == ["big.py"]
    f = result.findings[0]
    assert f.category == Category.BUG
    assert f.severity == Severity.HIGH
    assert f.line == 4  # 锚定定位把 existing_code 钉到新侧第 4 行
    assert f.side == "RIGHT"


def test_review_unlocatable_finding_stays_line_none():
    wanted = {
        "summary": "s",
        "scores": {},
        "findings": [
            {"content": "c", "category": "bug", "severity": "medium",
             "file": "app.py", "existing_code": "def does_not_exist_anywhere():"}
        ],
        "skipped_files": [],
    }

    async def backend(messages):
        return json.dumps(wanted, ensure_ascii=False)

    r = _fake_reviewer(backend)

    async def run():
        return await r.review(pr=_pr(), commits_text="", diffs=[_diffs()[0]])

    result = asyncio.run(run())
    assert result.findings[0].line is None  # 上层可据此降级入总结评论

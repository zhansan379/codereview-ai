"""forges/base 测试：repo_path_from_url 从仓库链接解析 owner/repo（或 GitLab 嵌套 namespace/path）。

纯函数、离线，供「新增项目」从 URL 自动回填 repo_id/repo_full_name。
"""

from __future__ import annotations

from codereview_ai.forges.base import repo_path_from_url


def test_github_style_owner_repo():
    assert repo_path_from_url("https://github.com/o/r", "github") == "o/r"
    assert repo_path_from_url("https://github.com/o/r.git", "github") == "o/r"
    assert repo_path_from_url("https://github.com/o/r/pull/1", "github") == "o/r"
    assert repo_path_from_url("https://github.com/o/r/tree/main", "github") == "o/r"


def test_gitee_gitea_use_generic_owner_repo():
    assert repo_path_from_url("https://gitee.com/o/r", "gitee") == "o/r"
    assert repo_path_from_url("https://gitea.example.com/team/repo.git", "") == "team/repo"


def test_gitlab_nested_namespace():
    assert repo_path_from_url("https://gitlab.com/group/sub/repo/-/merge_requests/1", "gitlab") == "group/sub/repo"
    assert repo_path_from_url("https://gitlab.com/group/repo", "gitlab") == "group/repo"
    assert repo_path_from_url("https://gitlab.com/group/repo/-/blob/main/x.py", "gitlab") == "group/repo"


def test_unparseable_returns_empty():
    assert repo_path_from_url("", "github") == ""
    assert repo_path_from_url("not-a-url", "github") == ""
    assert repo_path_from_url("https://example.com/onlyone", "github") == ""
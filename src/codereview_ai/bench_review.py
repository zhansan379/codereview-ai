"""AACR-Bench 无头评审入口：对「本地已 checkout 的 base..head 工作区」跑 agentic 审查引擎。

被 `aacr-bench/evaluation/reviewers/codereview.py` 以子进程方式调用（OCR 式外壳契约）：

    CODEREVIEW_COMMAND review --repo <aacr工作区> --base <base> --head <head> \
        --cache-dir <缓存> --format json

本模块"无 Settings/DB 依赖"：LLM 配置直接从环境变量捏造 `ResolvedLLM`，**不实例化
`config.settings.Settings`**（其 `_fail_fast` 缺 4 个应用密钥会 SystemExit），也不碰
`ConfigRepository`（需要 engine + encryption_key）。仓库复用 aacr-bench 已 prepare 的本地
工作区：用 `RepoCloner` 把它 `git clone --bare` 进 codereview 自己的缓存，agentic 引擎按
`head_sha` 寻址 git 对象读取，不二次走网络、不依赖 forge 适配器。

结果以 JSON 打到 stdout（机器消费，成功即 `status:"ok"`）：

    {"findings":[{"file","content","line","start_line","end_line","old_line","side",
     "category","severity","source"}],
     "token_usage":{"input_tokens":N,"output_tokens":N}, "status":"ok", "error":""}

`--offline` 为不联网自测：合成一条 Finding 走同一序列化路径，验证装配正确。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codereview_ai.config.repository import ResolvedLLM
from codereview_ai.domain.models import (
    Category,
    FileDiff,
    Finding,
    PullRequest,
    Severity,
)
from codereview_ai.forges.base import (
    change_type_from_flags,
    count_diff_stats,
    new_file_content_from_patch,
)
from codereview_ai.review.agentic import llm_adapter
from codereview_ai.review.agentic.llmloop import AgentConfig
from codereview_ai.review.agentic.sandbox import run_agentic_review
from codereview_ai.review.agentic.syncer import RepoCloner, slugify_key
from codereview_ai.review.agentic.tools import RepoContext
from codereview_ai.review.file_scope import filter_review_scope

logger = logging.getLogger("codereview_ai.bench_review")

VERSION = "0.1.0"

#: git diff 输出按文件切块的头部，捕获 old/new 两段路径。
_DIFFSPLIT_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$", re.MULTILINE)


class ConfigError(RuntimeError):
    """配置缺失/非法（live 评审需要 LLM）。"""


class _LocalRuntime:
    """把已 bare-clone 进缓存的本地仓库镜像成只读工作区（aacr 已 prepare，复用不重下网）。

    与产品的 `LocalCloneRuntime` 同构，但仓库同步由调用方先行完成（`repo_dir` 传入），
    这里不再 `git_clone_url(pr)`（它只接受 http(s)）。夹具给 `run_agentic_review` 用：
    工具层按 `pinned_sha` 只读寻址 git 对象。
    """

    def __init__(self, repo_dir: Path, head_sha: str) -> None:
        self._repo_dir = repo_dir
        self._head = head_sha

    async def guard(self) -> None:
        if not RepoCloner.available():
            raise RuntimeError("未找到 git 可执行文件")

    async def start(self, pr: PullRequest | None, diffs: list[FileDiff]) -> RepoContext:
        await self.guard()
        diff_map = {d.new_path.removeprefix("/"): d.diff for d in diffs}
        return RepoContext(
            workspace=self._repo_dir,
            diff_map=diff_map,
            repo_dir=self._repo_dir,
            pinned_sha=self._head,
        )

    async def stop(self) -> None:
        # 保留缓存供后续复用；无 working tree 可清
        return


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def default_cache_dir() -> Path:
    """agent bare-clone 缓存根；`CR_BENCH_CACHE_DIR` 可覆盖，缺省系统临时目录。"""
    env = (os.environ.get("CR_BENCH_CACHE_DIR") or "").strip()
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "codereview-bench-cache"


def build_resolved_llm() -> ResolvedLLM:
    """从 env 直接捏造 `ResolvedLLM`，不实例化 Settings（无 4 密钥也能跑）。

    读取：`CR_LLM_MODEL`（必填）/ `CR_LLM_API_KEY` / `CR_LLM_BASE_URL` / `CR_LLM_MAX_TOKENS`。
    """
    model = (os.environ.get("CR_LLM_MODEL") or "").strip()
    if not model:
        raise ConfigError("CR_LLM_MODEL 未设置（live 评审需要指定 LLM 模型）")
    max_tokens = None
    raw = (os.environ.get("CR_LLM_MAX_TOKENS") or "").strip()
    if raw:
        try:
            max_tokens = int(raw)
        except ValueError:
            max_tokens = None
    return ResolvedLLM(
        name=model,
        provider="",
        model=model,
        api_key=(os.environ.get("CR_LLM_API_KEY") or "").strip(),
        base_url=(os.environ.get("CR_LLM_BASE_URL") or "").strip(),
        max_tokens=max_tokens,
        temperature=None,
    )


class _TokenCounter:
    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0


def _counting_factory(resolved: ResolvedLLM, counter: _TokenCounter) -> Callable[[], Any]:
    """包一个计数 backend 的 `llm_factory`：每轮 LLM 调用累计真实 usage（无 usage 则跳过）。"""
    base = llm_adapter._litellm_tool_backend(  # noqa: SLF001 —— 包计数 backend 复用默认实现
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        max_tokens=resolved.max_tokens,
        temperature=resolved.temperature,
    )

    async def _counting(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        resp = await base(messages, tools)
        usage = llm_adapter._usage(resp)  # noqa: SLF001
        if usage:
            counter.prompt += int(usage.get("prompt_tokens") or 0)
            counter.completion += int(usage.get("completion_tokens") or 0)
        return resp

    def factory() -> Any:
        return llm_adapter.ToolCallingLLM(model=resolved.model, backend=_counting)

    return factory


# ---------------------------------------------------------------------------
# 纯本地 git -> FileDiff
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, args: list[str], *, timeout: int = 180) -> str:
    """在 `repo_dir`(git 仓库根) 跑只读 git 子命令，超时/失败抛 RuntimeError。

    Windows 下必须显式 `encoding="utf-8"`（默认 GBK 会炸 reader 线程但 child 仍退出 0）。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args[:2])} 超时（>{timeout}s）") from exc
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"git 运行失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{detail[:200]}")
    return proc.stdout


def _clean_path(p: str) -> str:
    """去掉 git 路径占位 `/dev/null` 与首尾空白；保留仓库相对路径。"""
    p = p.strip()
    if p in ("/dev/null", "dev/null") or p.endswith(("/dev/null", "dev/null")):
        return "/dev/null"
    return p


def build_file_diffs(repo_dir: Path, base: str, head: str) -> list[FileDiff]:
    """由本地仓库的 `merge-base(base, head)..head` diff 构建 `list[FileDiff]`。

    范围语义 = 三点 `A...B`（git 内部按 merge-base 解析），与 OCR `diff/git.go`
    ModeRange（`base = merge-base(from,to)`）及 forge compare API（GitHub/GitLab
    的 PR diff 同样是 merge-base 口径）一致。两点 `A B` 会把分叉点之后的所有历史
    变更都算进来——cline 实例实测：两点 276 文件 vs 三点 4 文件（base 不是 head
    祖先时差量爆炸，整次审查被拖死）。

    复用产品 `forges.base` 的 `count_diff_stats` / `new_file_content_from_patch` /
    `change_type_from_flags`（与 GitHub/GitLab 适配器同构的内部还原）。
    """
    raw = _git(repo_dir, ["-c", "diff.renames=true", "diff", "--no-color", "-M",
                          f"{base}...{head}", "--"])
    if not raw:
        return []
    # split 结果：blocks[0]=头部前文本，之后每组 (old, new, body) 交替。
    blocks = re.split(_DIFFSPLIT_RE, raw)
    diffs: list[FileDiff] = []
    for idx in range(1, len(blocks), 3):
        old, new, body = blocks[idx], blocks[idx + 1], blocks[idx + 2]
        old = _clean_path(old)
        new = _clean_path(new or old)
        if new == old == "/dev/null":
            continue
        additions, deletions = count_diff_stats(body)
        # 提交级 `git diff A B`：新增/删除文件在 body 里是 `new/deleted file mode`，而非
        # `/dev/null`（后者只在其它模式混用时出现）。两者都判，兼容两类形态。
        is_new = old == "/dev/null" or "new file mode" in body
        is_deleted = new == "/dev/null" or "deleted file mode" in body
        renamed = "similarity index" in body
        change_type = change_type_from_flags(
            is_new=is_new, is_deleted=is_deleted, is_renamed=renamed
        )
        new_content = "" if is_deleted else new_file_content_from_patch(body, change_type)
        diffs.append(
            FileDiff(
                old_path=old,
                new_path=new,
                diff=body,
                additions=additions,
                deletions=deletions,
                change_type=change_type,
                new_file_content=new_content,
            )
        )
    return diffs


# ---------------------------------------------------------------------------
# 结果序列化
# ---------------------------------------------------------------------------


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    """`Finding` -> aacr 结果 finding。

    side 转小写；line/end_line 取与锚定同侧的行号区间（RIGHT 用新侧、LEFT 用旧侧）：
    命中块区间用于定位评测（对齐 OCR 的 start_line/end_line 区间语义）；未锚定
    （end_line 缺省）时退化为 `line` 单点。
    """
    side = (f.side or "RIGHT").upper()
    line = f.line if side == "RIGHT" else f.old_line
    end_line = f.end_line if f.end_line is not None else line
    cat = getattr(f.category, "value", None)
    sev = getattr(f.severity, "value", None)
    return {
        "file": f.file,
        "content": f.content,
        "line": line,
        "start_line": line,
        "end_line": end_line,
        "old_line": f.old_line,
        "side": side.lower(),
        "category": str(cat) if cat else "",
        "severity": str(sev) if sev else "",
        "source": f.source,
    }


def findings_envelope(
    findings: list[Finding],
    token_usage: dict[str, int],
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # `warnings` 只做可观测性：工具层参数被修复/整批或条目被丢时留痕，让「这条样本
    # 为什么只有 N 条评论」在结果里可查（外层适配器只读 findings/token_usage，多这个键无影响）。
    return {
        "findings": [_finding_to_dict(f) for f in findings],
        "token_usage": {
            "input_tokens": int(token_usage.get("input_tokens", 0)),
            "output_tokens": int(token_usage.get("output_tokens", 0)),
        },
        "warnings": warnings or [],
    }


def _print(payload: dict[str, Any], *, ok: bool, error: str = "") -> int:
    out = dict(payload)
    out.update({"status": "ok" if ok else "error", "error": error})
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 2


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def _cmd_offline() -> int:
    """不联网自测：合成一条 Finding 走真实序列化路径，验证入口装配。"""
    demo = Finding(
        content="[offline smoke] 示例问题：资源未释放。",
        category=Category.BUG,
        severity=Severity.MEDIUM,
        existing_code="x = open(f)  # require close",
        file="demo.py",
        line=1,
        side="RIGHT",
        source="agent",
    )
    return _print(findings_envelope([demo], {}), ok=True)


async def _cmd_review(args: argparse.Namespace, repo_path: str) -> int:
    """live：对 `base..head` 跑 agentic 审查，输出 findings JSON。

    `repo_path` 由主流程（同步脚段）归一成绝对路径后传入，避免 async 内做阻塞 stat。
    """
    cache_dir = Path(args.cache_dir) if args.cache_dir else default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    pr = PullRequest(
        provider="github",
        repo_id=args.repo_full_name or "local/repo",
        repo_full_name=args.repo_full_name or "local/repo",
        web_url=args.clone_url or f"http://localhost/{args.repo_full_name or 'local/repo'}.git",
        pr_number=0,
        title="",
        source_branch=args.head,
        target_branch=args.base,
        head_sha=args.head,
        base_sha=args.base,
    )

    owner_repo = args.repo_full_name or "local/repo"

    try:
        # bare-clone 本地工作区进缓存（幂等 fetch），再算 base..head 的 FileDiff。
        cloner = RepoCloner(cache_dir)
        repo_dir = await asyncio.to_thread(
            cloner.sync_to,
            url=repo_path,
            key=slugify_key(owner_repo),
            ref=args.head,
        )
        diffs = await asyncio.to_thread(build_file_diffs, repo_dir, args.base, args.head)
        # 默认审查范围对齐 OCR（扩展名白名单 + 测试/生成物排除）：基准口径 = 生产口径
        diffs, scope_skipped = filter_review_scope(diffs)
        if scope_skipped:
            logger.info("默认审查范围跳过 %d 个文件：%s", len(scope_skipped), scope_skipped)
    except Exception as exc:  # noqa: BLE001  —— 整理成可读 JSON 报错
        return _print({}, ok=False, error=f"仓库同步/取 diff 失败：{exc}")

    if args.preview:
        # 只走 git 装配（sync + diff），不碰 LLM 配置、不调用模型。
        return _print(findings_envelope([], {}), ok=True)

    try:
        resolved = build_resolved_llm()
    except ConfigError as exc:
        return _print({}, ok=False, error=str(exc))

    try:
        counter = _TokenCounter()
        runtime = _LocalRuntime(repo_dir, args.head)
        # 分组与生产一致（bootstrap.py 同样构造）：用 CR_LLM_* 建真实 LLM 语义分组
        # （OCR 同构：files≥4 走 LLM 分组，<4 文件按 churn 决定整包/per-file；
        #  LLM 不可用降级 per-file）。见 review/grouping.py 的 OCR 决策链。
        from codereview_ai.review.group_gateway import (
            DEFAULT_GROUPING_MAX_TOKENS,
            LLMGroupAdapter,
        )
        from codereview_ai.review.grouping import SemanticGrouper
        from codereview_ai.review.llm_gateway import LLMGateway

        grouper = SemanticGrouper(LLMGroupAdapter(LLMGateway(
            model=resolved.model,
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            max_tokens=resolved.max_tokens or DEFAULT_GROUPING_MAX_TOKENS,
            temperature=resolved.temperature,
        )))

        _timeout = max(60.0, float(args.timeout_minutes) * 60.0)
        # 单组上限 = 整次预算（组内还会被全局剩余预算二次收紧）：大 PR 到点即停、
        # 保留已得结果（否则会被外层进程超时强杀、零产出——cline 3.9 万 churn 实测）。
        _cfg: dict[str, Any] = {
            "max_time_seconds": _timeout,
            "run_budget_seconds": _timeout,
        }
        # 会话预算（轮数 / 提示 token / 组并发 / plan 阈值 / 阶段开关）此前只落到
        # AgentConfig 的 dataclass 默认（20 轮 / 80k / plan 300-600），而生产路径
        # bootstrap.py 是读 CR_AGENT_* 的——同一份代码在基准与生产跑出不同深度，
        # 基准侧调 CR_AGENT_* 也不生效。这里接上同名环境变量；不设时保持原默认值。
        for _field, _env, _cast in (
            ("max_iterations", "CR_AGENT_MAX_ITERATIONS", int),
            ("max_prompt_tokens", "CR_AGENT_MAX_PROMPT_TOKENS", int),
            ("group_concurrency", "CR_AGENT_GROUP_CONCURRENCY", int),
            ("plan_line_threshold", "CR_AGENT_PLAN_LINE_THRESHOLD", int),
            ("plan_group_line_threshold", "CR_AGENT_PLAN_GROUP_LINE_THRESHOLD", int),
            ("plan_enabled", "CR_AGENT_PLAN_ENABLED", lambda v: v.lower() in ("1", "true", "yes")),
            ("relocation_enabled", "CR_AGENT_RELOCATION_ENABLED",
             lambda v: v.lower() in ("1", "true", "yes")),
            ("scoring_enabled", "CR_AGENT_SCORING_ENABLED", lambda v: v.lower() in ("1", "true", "yes")),
        ):
            _raw = os.environ.get(_env)
            if _raw:
                _cfg[_field] = _cast(_raw)
        # CR_BENCH_NO_FILTER=1 关闭 review_filter 事实核查阶段：默认它只删「diff 铁证反驳
        # 的错评」，但这可能导致 agent 对「轻微但真值要报」的问题更保守。关闭用于验证
        # 漏报是否由 review_filter 造成（其余 AgentConfig 保持默认）。
        if os.environ.get("CR_BENCH_NO_FILTER") == "1":
            _cfg["review_filter_enabled"] = False
        result = await run_agentic_review(
            runtime,
            _counting_factory(resolved, counter),
            diffs,
            pr,
            grouper=grouper,
            cfg=AgentConfig(**_cfg),
        )
    except Exception as exc:  # noqa: BLE001  —— 引擎任一步骤失败 → status:error（不中断进程）
        return _print({}, ok=False, error=f"agentic 审查失败：{exc}")

    return _print(
        findings_envelope(
            result.findings,
            {"input_tokens": counter.prompt, "output_tokens": counter.completion},
            result.warnings,
        ),
        ok=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codereview_ai.bench_review", description="AACR-Bench 无头评审"
    )
    # 外壳契约常带 `review` 子命令词（aacr adapter 传 `CODEREVIEW_COMMAND review ...`）；
    # 也允许省略（`... --repo ...` 直接用）。二者等价，忽略其取值即可。
    parser.add_argument(
        "cmd", nargs="?", default="review", choices=["review"], help=argparse.SUPPRESS
    )
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    parser.add_argument("--offline", action="store_true", help="不联网自测（合成一条 finding）")
    parser.add_argument(
        "--repo", default="", help="本地已 checkout 仓库路径（aacr prepare 的工作区）"
    )
    parser.add_argument(
        "--repo-full-name", default="", help="owner/name（缓存 key；缺省 local/repo）"
    )
    parser.add_argument("--clone-url", default="", help="原始 clone 地址（仅存档/展示）")
    parser.add_argument("--base", default="", help="diff 起点 commit")
    parser.add_argument("--head", default="", help="diff 终点 commit（被审代码状态）")
    parser.add_argument(
        "--cache-dir", default="", help="bare-clone 缓存根（缺省 CR_BENCH_CACHE_DIR/临时目录）"
    )
    parser.add_argument("--timeout-minutes", type=float, default=30.0, help="单条评审硬超时（分）")
    parser.add_argument(
        "--preview", action="store_true", help="只做 git 装配（sync+diff），不调用 LLM"
    )
    parser.add_argument("--format", default="json", help="输出格式（当前仅 json）")
    return parser


def _force_utf8_stdout() -> None:
    """把 stdout 切成 UTF-8（管道/重定向同理）：避免 Windows ANSI 代码页把 JSON 打成 GBK，
    让外层（aacr adapter 按 utf-8 读）拿到真正的 UTF-8 字节。无 reconfigure 则忽略。"""
    if not hasattr(sys.stdout, "reconfigure"):
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"codereview_ai.bench_review {VERSION}")
        return 0
    if args.format != "json":
        print(
            json.dumps(
                {"status": "error", "error": f"不支持的输出格式：{args.format}"}, ensure_ascii=False
            )
        )
        return 2
    if args.offline:
        return _cmd_offline()
    if not args.repo or not args.base or not args.head:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "live 评审需要 --repo/--base/--head；可用 --offline 自测",
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        repo_path = os.path.abspath(args.repo)
        return asyncio.run(_cmd_review(args, repo_path))
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底，保证恒有 JSON 可解析
        return _print({}, ok=False, error=str(exc))


if __name__ == "__main__":
    raise SystemExit(main())

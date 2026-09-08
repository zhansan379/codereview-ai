"""真实调用 code_comment 工具的 __main__ 演示（不发散到服务器）。

真正走一次生产链路的关键一环：从 DB/env 解析出当前 LLM → 用 `ToolCallingLLM`（自带
工具调用 adapter）发起真实 chat → 把模型实际交回的 tool_calls 跑过 `ToolRunner` →
看 code_comment 的意见被收进几条、丢几条、为什么丢。用来验证"标准嵌套 schema 之后
模型是否照着填"——这是之前 9 次 code_comment→0 意见的根因修复。

不 clone 全仓，只物化一个带 bug 的小文件给模型读；建议发给模型"立刻用 code_comment
补交意见"，让它这一轮就产生真实 code_comment 调用。诊断日志照常打在 stdout。

用法（在仓库根目录）：
  .venv/Scripts/python.exe -X utf8 scripts/demo_code_comment.py
需 CR_ 密钥齐全（Settings() 会 fail-fast），且 DB/env 里有可用 LLM 模型。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from codereview_ai.config.repository import ConfigRepository
from codereview_ai.config.settings import Settings
from codereview_ai.review.agentic.llm_adapter import build_agent_llm
from codereview_ai.review.agentic.tools import RepoContext, ToolRunner, ToolState, tool_schemas
from codereview_ai.storage.db import create_engine

# 一段明显有问题的代码，让模型有内容可批。
BAD_FILE = '''import subprocess

def run_cmd(user_input):
    cmd = "ls " + user_input          # ① os 注入：拼 shell 字符串
    return subprocess.run(cmd, shell=True)  # ② shell=True + 拼接 = RCE

def divide(a, b):
    return a / b                      # ③ 未处理 b==0

run_cmd("--help")
'''

INTRO = ("这是待审查仓库里一个文件的一段代码，路径是 demo.py。请直接调用 code_comment "
         "上报你发现的问题（可以多条），然后用 task_done 结束。不用先读太多，内容就这些。")


async def main() -> None:
    settings = Settings()
    # 与生产同一路径解析 LLM：DB model_config 优先，env 重放压 DB。
    engine = create_engine(settings.database_url)
    try:
        repo = ConfigRepository(engine, encryption_key=settings.encryption_key)
        resolved = await repo.resolve_llm()
        llm = build_agent_llm(resolved)
        if llm is None:
            print("没有可用 LLM 模型（DB model_config / CR_LLM_MODEL）。")
            return
        print(f"模型: {resolved.model}")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "demo.py").write_text(BAD_FILE, "utf-8")
            runner = ToolRunner(RepoContext(workspace=ws, group_key="demo.py"), ToolState())

            # 收一个真实回合：模型可能先 read_file 再 code_comment，最多跑几轮直到 task_done。
            messages: list[dict] = [
                {"role": "system", "content": "你是一个代码审查 agent。只能调给定只读工具与 code_comment。"},
                {"role": "user", "content": INTRO},
            ]
            for _ in range(6):
                turn = await llm.chat(messages, tool_schemas())
                messages.append({
                    "role": "assistant",
                    "content": turn.content or None,
                    "tool_calls": [
                        {"id": tc.id or f"c{i}", "type": "function",
                         "function": {"name": tc.name,
                                      "arguments": tc.raw_arguments or
                                      __import__("json").dumps(tc.args)}}
                        for i, tc in enumerate(turn.tool_calls)
                    ],
                })
                for tc in turn.tool_calls:
                    result = runner.run_one(tc.name, tc.args)
                    messages.append({"role": "tool", "tool_call_id": tc.id or "x",
                                     "name": tc.name, "content": str(result)})
                    print(f"  → {tc.name}: {str(result)[:80]}")
                    if runner.state.done:
                        break
                if runner.state.done:
                    break

            comments = runner.state.comments
            print(f"\n收进 {len(comments)} 条意见：")
            for f in comments:
                print(f"  [{f.severity.name}/{f.category.name}] {f.file}:{f.line} — {f.content[:60]}")
            print(f"丢弃样例请看上方 code_comment 诊断行（无 WARNING 即全部收下）。")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
"""pytest 全局夹具：测试间环境隔离。

litellm 首次 import 时会 `load_dotenv()`，把仓库根 `.env` 里的 CR_LLM_*（本机真实
LLM 配置）灌进 `os.environ`；只要有任何测试先 import 了 litellm（如
`monkeypatch.setattr("litellm.acompletion", ...)`），这些键就会残留到后续用例，
使「env 未显式配置时 DB 优先」的断言随执行顺序随机失败。
"""

from __future__ import annotations

import os

import pytest

#: 会被 .env 灌入、且参与「env 是否显式配置」断言的键前缀。
_AMBIENT_ENV_PREFIXES = ("CR_LLM_",)


@pytest.fixture(autouse=True)
def _isolate_ambient_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉进程环境里残留的 CR_LLM_*（litellm load_dotenv 的副作用）。

    需要这些键的用例自行在用例内 `monkeypatch.setenv`（在本夹具之后执行，不受影响）。
    """
    for key in [k for k in os.environ if k.startswith(_AMBIENT_ENV_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)

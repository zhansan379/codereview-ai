"""Token 计数与截断工具。

来源：AI-Codereview-Gitlab 的 biz/utils/token_util.py，本文件做了以下改造：
  1. 移除对项目内 logger 的依赖，改用标准 logging（便于直接复制到新项目）
  2. 编码器缓存改为 functools.lru_cache，去掉手写的全局变量 + 竞态
  3. truncate 增加 from_end 参数（diff 场景下有时保留尾部更有意义）
  4. 新增 truncate_with_notice：截断时留下显式提示，避免"静默丢内容"
     （原项目 code_reviewer.py:79-81 就是静默截断，用户完全不知道后面的文件没被 review）

可直接复制到新项目的 src/codereview_ai/utils/tokens.py。
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_ENCODING = "cl100k_base"


@lru_cache(maxsize=4)
def _get_encoding(encoding_name: str = DEFAULT_ENCODING):
    """获取 tiktoken 编码器；离线或加载失败时返回 None，由调用方降级。

    lru_cache 保证每个 encoding_name 只加载一次（加载耗时约 1-2s 且会联网下载词表）。
    """
    try:
        import tiktoken

        return tiktoken.get_encoding(encoding_name)
    except Exception as exc:  # 离线环境 / 词表下载失败 / tiktoken 未安装
        logger.warning("tiktoken encoding %r unavailable, falling back to estimation: %s", encoding_name, exc)
        return None


def _estimate_tokens(text: str) -> int:
    """离线兜底估算：中文约 2 字符/token，其他约 4 字符/token。

    这个经验值对 cl100k_base 足够准（误差通常 <15%），用于预算控制没问题，
    但不要用它做计费。
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - chinese
    return max(chinese // 2 + other // 4, 1)


def count_tokens(text: str, encoding_name: str = DEFAULT_ENCODING) -> int:
    """计算文本 token 数。tiktoken 不可用时自动降级为估算。"""
    if not text:
        return 0
    encoding = _get_encoding(encoding_name)
    if encoding is None:
        return _estimate_tokens(text)
    return len(encoding.encode(text))


def truncate_by_tokens(
    text: str,
    max_tokens: int,
    *,
    encoding_name: str = DEFAULT_ENCODING,
    from_end: bool = False,
) -> str:
    """把文本截断到 max_tokens 以内。

    Args:
        from_end: True 时保留**尾部**。审查 diff 时，文件末尾的改动往往比
                  文件头的 import 更值得看，这个开关偶尔有用。
    """
    if not text or max_tokens <= 0:
        return ""

    encoding = _get_encoding(encoding_name)
    if encoding is not None:
        tokens = encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text
        kept = tokens[-max_tokens:] if from_end else tokens[:max_tokens]
        return encoding.decode(kept)

    # 离线兜底：按字符比例估算
    chinese = sum(1 for c in text if "一" <= c <= "鿿")
    chars_per_token = 2 if chinese * 2 > len(text) else 4
    max_chars = max_tokens * chars_per_token
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] if from_end else text[:max_chars]


def truncate_with_notice(
    text: str,
    max_tokens: int,
    *,
    encoding_name: str = DEFAULT_ENCODING,
    notice: str = "\n\n... [内容过长已截断，剩余 {remaining} tokens 未包含] ...",
) -> tuple[str, bool]:
    """截断并附加显式提示。

    返回 (文本, 是否发生了截断)。

    为什么需要这个：原项目静默截断，LLM 不知道自己只看到了一部分，
    会对"缺失的部分"给出错误结论；用户也不知道后面的文件根本没被审查。
    把截断这件事**告诉模型也告诉用户**，是低成本高收益的改进。
    """
    total = count_tokens(text, encoding_name)
    if total <= max_tokens:
        return text, False

    notice_tokens = count_tokens(notice, encoding_name)
    body = truncate_by_tokens(text, max(max_tokens - notice_tokens, 1), encoding_name=encoding_name)
    return body + notice.format(remaining=total - max_tokens), True


def split_by_tokens(text: str, chunk_tokens: int, *, encoding_name: str = DEFAULT_ENCODING) -> list[str]:
    """把长文本切成若干不超过 chunk_tokens 的块，用于 map-reduce 审查。

    注意：这是纯 token 层面的切分，不理解 diff 结构。
    真正审查 diff 时应该**按文件/hunk 边界切**（见 DESIGN.md 的分片策略），
    这个函数只用于兜底处理单个超大文件。
    """
    if not text:
        return []
    encoding = _get_encoding(encoding_name)
    if encoding is None:
        chars = chunk_tokens * 4
        return [text[i : i + chars] for i in range(0, len(text), chars)] or [text]

    tokens = encoding.encode(text)
    return [encoding.decode(tokens[i : i + chunk_tokens]) for i in range(0, len(tokens), chunk_tokens)] or [text]

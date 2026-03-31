"""LLM-based one-sentence summary generation (title + abstract only)."""

from __future__ import annotations

import logging
import traceback
from typing import Callable

from paper_tool.config import get_config
from paper_tool.llm_stream import completion_to_text
from paper_tool.models import PaperMetadata
from paper_tool.retry import with_retry as _with_retry

log = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = """你是一位精通学术写作的计算机科学专家，擅长将复杂的论文摘要浓缩为信息密度极高的一句话。

Task: 请根据我提供的论文标题和摘要，总结出"一句话摘要"。

Constraints (严苛约束):
- 内容忠实性：所有定语和核心概念必须严格提取自原文，严禁引入外部术语或过度解读。
- 语法结构：使用"通过[手段/方法A]与[手段/方法B]，[实现了核心结论]"的结构。
- 信息密度：尽量将论文中提到的关键机制转化为名词短语嵌入到句子中，确保一句话涵盖"问题-方法-机制-结果"全过程。
- 语言要求：学术、专业、精炼，拒绝口语化表达。

直接输出一句话，不要包含任何前缀、解释或换行。"""


def _build_user_prompt(metadata: PaperMetadata) -> str:
    return f"**标题**: {metadata.title}\n\n**摘要**:\n{metadata.abstract}"


class LLMSummarizer:
    """Generates a single-sentence Chinese summary from paper title and abstract."""

    def __init__(self) -> None:
        self._cfg = get_config()

    def summarize(
        self,
        metadata: PaperMetadata,
        debug: bool = False,
        stream: bool = False,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        """
        Call LLM with title + abstract only, return one-sentence summary string.
        Raises on LLM failure so the caller can decide whether to skip.
        """

        def _dbg(label: str, content: str = "") -> None:
            log.debug("Summarizer · %s\n%s", label, (content or "(empty)")[:5000])
            if not debug:
                return
            sep = "-" * 60
            print(f"\n{sep}", flush=True)
            print(f"[DEBUG] Summarizer · {label}", flush=True)
            print(sep, flush=True)
            print(content or "(empty)", flush=True)

        system_prompt = self._cfg.summarizer_prompt or _DEFAULT_SYSTEM_PROMPT
        user_prompt = _build_user_prompt(metadata)

        _dbg("System Prompt", system_prompt)
        _dbg("User Prompt", user_prompt)

        kwargs: dict = {
            "model": self._cfg.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._cfg.llm_summarizer_max_tokens,
            "temperature": self._cfg.llm_temperature,
        }
        if self._cfg.openai_base_url:
            kwargs["api_base"] = self._cfg.openai_base_url

        stream_enabled = (stream or self._cfg.llm_stream_window) and on_token is None
        try:
            result = _with_retry(
                lambda: completion_to_text(
                    request_kwargs=kwargs,
                    stream=stream_enabled,
                    stream_title="LLM 流式输出 · 摘要",
                    stream_height=self._cfg.llm_stream_window_height,
                    on_token=on_token,
                ),
                max_attempts=3,
                base_delay=3.0,
            )
        except Exception:
            log.exception("Summarizer LLM call failed (all retries exhausted)")
            if debug:
                _dbg("LLM Call FAILED (all retries exhausted)")
                traceback.print_exc()
            raise

        raw = result.text
        finish_reason = result.finish_reason or (
            "stream" if stream_enabled else "unknown"
        )
        usage = result.usage
        usage_str = (
            f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} total={usage.total_tokens}"
            if usage
            else "N/A"
        )
        log.info(
            "Summarizer response  finish=%s  %s  raw=%d chars",
            finish_reason,
            usage_str,
            len(raw),
        )

        if debug:
            _dbg(f"Response Meta  finish_reason={finish_reason}  usage={usage_str}")
            _dbg(f"Raw Response ({len(raw)} chars)", raw)

        return raw.strip()

"""LLM-based paper note generation (analysis only, no classification)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from paper_tool.config import get_config
from paper_tool.models import FigureInfo, PaperMetadata, PaperNote
from paper_tool.retry import retry as _retry, with_retry as _with_retry

if TYPE_CHECKING:
    pass


_SYSTEM_PROMPT = """你是一位专业的学术论文分析助手，擅长深度理解和总结计算机科学、人工智能领域的研究论文。

你的任务是分析给定的论文内容，生成结构化的中文阅读笔记。

输出必须是合法的 JSON，结构如下：
{
  "overview": "一段话概括论文的核心内容和主要发现（200-300字）",
  "research_problem": "论文要解决什么问题，研究动机和背景（150-200字）",
  "methodology": "技术方案、模型架构、核心算法的详细描述（200-400字）",
  "contributions": [
    "贡献点1（具体描述）",
    "贡献点2（具体描述）",
    "贡献点3（具体描述）"
  ],
  "experiments": "数据集、基线方法、关键实验结果和指标（150-300字）",
  "limitations": "作者提到的局限性、潜在问题和未来工作方向（100-200字）",
  "key_takeaways": [
    "最值得记住的要点1",
    "最值得记住的要点2",
    "最值得记住的要点3"
  ]
}

要求：
- 所有内容用中文输出
- contributions 列表包含 3-5 个具体贡献点
- key_takeaways 列表包含 3-5 个最重要的结论或洞见
- 不要输出任何 JSON 以外的内容
- 确保 JSON 格式合法，字符串中的引号需要转义"""


def _build_user_prompt(metadata: PaperMetadata, paper_text: str) -> str:
    return f"""请分析以下论文：

**标题**: {metadata.title}
**作者**: {metadata.authors_str}
**摘要**: {metadata.abstract}

**论文正文（节选）**:
{paper_text}"""


def _parse_response(raw: str) -> PaperNote:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse LLM response as JSON:\n{raw[:500]}")
        data = json.loads(match.group())

    def _lst(key: str) -> list[str]:
        val = data.get(key, [])
        return [str(v) for v in val] if isinstance(val, list) else ([str(val)] if val else [])

    return PaperNote(
        overview=str(data.get("overview", "")),
        research_problem=str(data.get("research_problem", "")),
        methodology=str(data.get("methodology", "")),
        contributions=_lst("contributions"),
        experiments=str(data.get("experiments", "")),
        limitations=str(data.get("limitations", "")),
        key_takeaways=_lst("key_takeaways"),
    )


def _llm_call(system: str, user: str, *, max_tokens: int | None = None) -> str:
    """Single LLM call, returns raw response text. Raises on empty response."""
    import litellm

    cfg = get_config()
    kwargs: dict = {
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens or cfg.llm_translator_max_tokens,
        "temperature": cfg.llm_temperature,
    }
    if cfg.openai_base_url:
        kwargs["api_base"] = cfg.openai_base_url
    response = litellm.completion(**kwargs)
    choice = response.choices[0]

    raw = choice.message.content or ""
    if not raw.strip():
        raw = getattr(choice.message, "reasoning_content", None) or ""

    raw = raw.strip()
    if not raw:
        raise ValueError("LLM 返回了空响应（content 和 reasoning_content 均为空）")
    return raw


def _strip_json_fence(raw: str) -> str:
    """Remove optional ```json ... ``` fences from an LLM response."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


@_retry(max_attempts=3, base_delay=3.0)
def translate_captions(figures: list[FigureInfo]) -> list[FigureInfo]:
    """
    Translate figure captions to Chinese using a single LLM call.

    Returns a new list of FigureInfo with translated captions.
    Figures with empty captions are returned unchanged.
    Retries up to 3 times on LLM or JSON-parse failures.
    """
    to_translate = [(i, fig) for i, fig in enumerate(figures) if fig.caption.strip()]
    if not to_translate:
        return figures

    system = (
        "你是一位学术论文翻译专家。将给定的图片说明（figure caption）翻译成中文。\n"
        "要求：\n"
        "1. 保留数学公式、变量名、模型名称原样不变\n"
        "2. 专业术语可保留英文并附中文注释，如 attention（注意力）\n"
        "3. 翻译要准确、流畅、符合学术规范\n"
        "4. 以 JSON 数组返回，元素顺序与输入完全一致，只输出 JSON 不要有其他内容"
    )
    captions_input = json.dumps(
        [fig.caption for _, fig in to_translate], ensure_ascii=False
    )
    user = f"请翻译以下图片说明：\n{captions_input}"

    raw = _llm_call(system, user)
    translated: list[str] = json.loads(_strip_json_fence(raw))
    if not isinstance(translated, list) or len(translated) != len(to_translate):
        raise ValueError(
            f"LLM 返回了 {len(translated)} 条翻译，期望 {len(to_translate)} 条"
        )

    result = list(figures)
    for (orig_idx, fig), new_caption in zip(to_translate, translated):
        if isinstance(new_caption, str) and new_caption.strip():
            result[orig_idx] = replace(fig, caption=new_caption.strip())
    return result


_FIGURE_PLACEMENT_INSTRUCTION = """

## 图片插入规则

本论文包含以下图片，请在笔记中与该图片内容最相关的章节末尾，单独一行插入标记 `[FIGURE:N]`（N 为图片编号）。
每张图片只使用一次，标记必须独占一行，不要加任何其他内容。

{figures_list}"""

_TABLE_PLACEMENT_INSTRUCTION = """

## 表格插入规则

本论文包含以下表格，请在笔记中与该表格内容最相关的章节末尾，单独一行插入标记 `[TABLE:N]`（N 为表格编号）。
每张表格只使用一次，标记必须独占一行，不要加任何其他内容。

{tables_list}"""


class LLMAnalyzer:
    """Generates structured reading notes from paper content."""

    def __init__(self) -> None:
        self._cfg = get_config()

    def _truncate(self, text: str, token_budget: int) -> str:
        max_chars = token_budget * 4
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n[... 文档已截断 ...]"

    def analyze(
        self,
        metadata: PaperMetadata,
        paper_text: str,
        debug: bool = False,
        figures: list[FigureInfo] | None = None,
        tables: list[FigureInfo] | None = None,
    ) -> PaperNote:
        """
        Generate reading notes. Does NOT perform classification.

        note_format="json"     → parse JSON response into structured PaperNote
        note_format="freeform" → store raw model output in PaperNote.raw_content

        When `figures` is provided (freeform mode only), the system prompt is
        augmented with instructions to place [FIGURE:N] markers in the output.
        """
        import traceback
        import litellm

        def _dbg(label: str, content: str = "") -> None:
            if not debug:
                return
            sep = "-" * 60
            print(f"\n{sep}", flush=True)
            print(f"[DEBUG] Analyzer · {label}", flush=True)
            print(sep, flush=True)
            print(content or "(empty)", flush=True)

        reserved = 2000
        truncated = self._truncate(paper_text, self._cfg.llm_max_input_tokens - reserved)
        system_prompt = self._cfg.analyzer_prompt or _SYSTEM_PROMPT

        if self._cfg.llm_note_format == "freeform":
            if figures:
                figures_list = "\n".join(
                    f"- 图片 {fig.number}：{fig.caption or '（无说明）'}"
                    for fig in figures
                )
                system_prompt += _FIGURE_PLACEMENT_INSTRUCTION.format(
                    figures_list=figures_list,
                )
            if tables:
                tables_list = "\n".join(
                    f"- 表格 {tbl.number}：{tbl.caption or '（无说明）'}"
                    for tbl in tables
                )
                system_prompt += _TABLE_PLACEMENT_INSTRUCTION.format(
                    tables_list=tables_list,
                )

        user_prompt = _build_user_prompt(metadata, truncated)

        _dbg("System Prompt", system_prompt)
        _dbg(
            f"User Prompt (正文截断至 {len(truncated):,} 字符，此处只显示前 1000 字符)",
            user_prompt[:1000],
        )

        kwargs: dict = {
            "model": self._cfg.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._cfg.llm_max_output_tokens,
            "temperature": self._cfg.llm_temperature,
        }
        if self._cfg.openai_base_url:
            kwargs["api_base"] = self._cfg.openai_base_url

        try:
            response = _with_retry(
                lambda: litellm.completion(**kwargs), max_attempts=3, base_delay=3.0,
            )
        except Exception:
            if debug:
                _dbg("LLM Call FAILED (all retries exhausted)")
                traceback.print_exc()
            raise

        choice = response.choices[0]
        raw = choice.message.content or ""

        # Some thinking models (e.g. kimi-k2.5) put output in reasoning_content
        if not raw:
            raw = getattr(choice.message, "reasoning_content", None) or ""

        if debug:
            finish_reason = getattr(choice, "finish_reason", "unknown")
            usage = getattr(response, "usage", None)
            usage_str = (
                f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} total={usage.total_tokens}"
                if usage else "N/A"
            )
            _dbg(
                f"Response Meta  finish_reason={finish_reason}  usage={usage_str}",
            )
            _dbg(f"Raw Response ({len(raw)} chars)", raw)

        if self._cfg.llm_note_format == "freeform":
            if metadata.source == metadata.source.ARXIV:
                alphaxiv_url = f"https://alphaxiv.org/abs/{metadata.paper_id}"
                bookmark = f"[🔖 alphaXiv · {metadata.paper_id}]({alphaxiv_url})\n\n"
                raw = bookmark + raw
            return PaperNote(raw_content=raw)

        return _parse_response(raw)

"""LLM-based paper note generation (analysis only, no classification)."""

from __future__ import annotations

import json
import re

from paper_tool.config import get_config
from paper_tool.models import PaperMetadata, PaperNote


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
    ) -> PaperNote:
        """
        Generate reading notes. Does NOT perform classification.

        note_format="json"     → parse JSON response into structured PaperNote
        note_format="freeform" → store raw model output in PaperNote.raw_content
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
            response = litellm.completion(**kwargs)
        except Exception:
            if debug:
                _dbg("LLM Call FAILED")
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

"""
LLM-based paper classification (decoupled from note generation).

Uses only title + abstract for speed. Selects from existing Notion options
and can propose new options following the existing naming style.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from paper_tool.config import get_config
from paper_tool.llm_stream import completion_to_text
from paper_tool.models import Classification, PaperMetadata
from paper_tool.retry import with_retry as _with_retry

_SYSTEM_PROMPT_TEMPLATE = """你是一位学术论文分类助手，负责为论文打标签。

你需要根据论文标题和摘要，为论文选择分类标签。每个字段都有现有的候选选项。

规则：
1. **优先复用现有选项**：尽量从候选列表中选择最匹配的，保持标签体系一致
2. **允许创建新选项**：如果现有选项确实无法覆盖（而不是勉强能对应），可以提出新选项
3. **新选项风格要求**：
   - 论文类型：简短英文词（1-2个词），如 "Survey"、"Framework"
   - 研究领域：英文关键词或缩写（1-2个词），如 "RLHF"、"SafetyAlignment"、"VisionLM"
   - 来源机构：机构名称，如 "Meta"、"DeepMind"、"CMU"
4. 每个字段返回 selected（从现有选项选）和 new（需要新建的选项）两个列表
5. selected 中的值必须与现有选项完全一致（包括大小写）

现有候选选项：
- 论文类型（paper_type）：{paper_type_opts}
- 研究领域（research_areas）：{research_areas_opts}
- 来源机构（institutions）：{institutions_opts}

输出必须是合法的 JSON，结构如下：
{{
  "paper_type": {{
    "selected": ["从现有选项中选，1-2个"],
    "new": ["如需新建则填，否则空列表"]
  }},
  "research_areas": {{
    "selected": ["从现有选项中选，1-4个"],
    "new": ["如需新建则填，否则空列表"]
  }},
  "institutions": {{
    "selected": ["从现有选项中选"],
    "new": ["如需新建则填，否则空列表"]
  }}
}}

不要输出任何 JSON 以外的内容。"""


def _build_system_prompt(
    options: dict[str, list[str]], template: str | None = None
) -> str:
    """
    Build classifier system prompt.
    Uses provided template (from prompts/classifier.md) or falls back to the built-in template.
    The options list is appended as a separate block so the prompt file stays static.
    """

    def _fmt(opts: list[str]) -> str:
        return "、".join(f'"{o}"' for o in opts) if opts else "（暂无）"

    if template is not None:
        # External prompt file: append options block dynamically
        options_block = f"""
现有候选选项：
- 论文类型（paper_type）：{_fmt(options.get("paper_type", []))}
- 研究领域（research_areas）：{_fmt(options.get("research_areas", []))}
- 来源机构（institutions）：{_fmt(options.get("institutions", []))}"""
        return template + options_block

    # Built-in template with format placeholders
    return _SYSTEM_PROMPT_TEMPLATE.format(
        paper_type_opts=_fmt(options.get("paper_type", [])),
        research_areas_opts=_fmt(options.get("research_areas", [])),
        institutions_opts=_fmt(options.get("institutions", [])),
    )


def _build_user_prompt(metadata: PaperMetadata) -> str:
    return f"""请对以下论文进行分类：

**标题**: {metadata.title}
**作者**: {metadata.authors_str}
**摘要**: {metadata.abstract}"""


def _parse_response(raw: str, available: dict[str, list[str]]) -> Classification:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse classifier response:\n{raw[:500]}")
        data = json.loads(match.group())

    def _merge(key: str) -> list[str]:
        field = data.get(key, {})
        if not isinstance(field, dict):
            # Fallback: if LLM returned a flat list instead of {selected, new}
            return [str(v) for v in field] if isinstance(field, list) else []

        selected = [str(v) for v in field.get("selected", [])]
        new_opts = [str(v) for v in field.get("new", [])]

        # Validate: selected values must exist in available options (case-insensitive fallback)
        valid_selected: list[str] = []
        avail_lower = {o.lower(): o for o in available.get(key, [])}
        for v in selected:
            if v in available.get(key, []):
                valid_selected.append(v)
            elif v.lower() in avail_lower:
                valid_selected.append(avail_lower[v.lower()])
            # else: silently drop invalid selected values

        return valid_selected + new_opts

    return Classification(
        paper_type=_merge("paper_type"),
        research_areas=_merge("research_areas"),
        institutions=_merge("institutions"),
    )


class LLMClassifier:
    """
    Classifies papers into Notion tags using title + abstract only.
    Completely independent of LLMAnalyzer.
    """

    def __init__(self) -> None:
        self._cfg = get_config()

    def classify(
        self,
        metadata: PaperMetadata,
        available_options: dict[str, list[str]],
        debug: bool = False,
        stream: bool = False,
        on_token: "Callable[[str], None] | None" = None,
    ) -> Classification:
        """
        Classify paper using only metadata (title, abstract, authors).
        Can propose new options when existing ones don't fit.

        available_options: {"paper_type": [...], "research_areas": [...], "institutions": [...]}
        """
        import traceback

        def _dbg(label: str, content: str = "") -> None:
            if not debug:
                return
            sep = "-" * 60
            print(f"\n{sep}", flush=True)
            print(f"[DEBUG] Classifier · {label}", flush=True)
            print(sep, flush=True)
            print(content or "(empty)", flush=True)

        classifier_template = self._cfg.classifier_prompt
        system_prompt = _build_system_prompt(available_options, classifier_template)
        user_prompt = _build_user_prompt(metadata)

        _dbg("System Prompt", system_prompt)
        _dbg("User Prompt", user_prompt)

        kwargs: dict = {
            "model": self._cfg.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self._cfg.llm_classifier_max_tokens,
            "temperature": self._cfg.llm_temperature,
        }
        if self._cfg.openai_base_url:
            kwargs["api_base"] = self._cfg.openai_base_url

        messages: list[dict] = kwargs.pop("messages")
        max_attempts = 3
        last_exc: Exception = RuntimeError("unreachable")
        stream_enabled = (stream or self._cfg.llm_stream_window) and on_token is None

        for attempt in range(max_attempts):
            if attempt > 0:
                _dbg(
                    f"Retry {attempt}/{max_attempts - 1} — feeding parse error back to model"
                )

            try:
                response = _with_retry(
                    lambda: completion_to_text(
                        request_kwargs={**kwargs, "messages": messages},
                        stream=stream_enabled,
                        stream_title=f"LLM 流式输出 · 分类 (attempt {attempt + 1})",
                        stream_height=self._cfg.llm_stream_window_height,
                        on_token=on_token,
                    ),
                    max_attempts=3,
                    base_delay=3.0,
                )
            except Exception:
                if debug:
                    _dbg("LLM Call FAILED (all retries exhausted)")
                    traceback.print_exc()
                raise

            raw = response.text

            if debug:
                finish_reason = response.finish_reason or (
                    "stream" if stream_enabled else "unknown"
                )
                usage = response.usage
                usage_str = (
                    f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} total={usage.total_tokens}"
                    if usage
                    else "N/A"
                )
                _dbg(
                    f"Response Meta  attempt={attempt}  finish_reason={finish_reason}  usage={usage_str}"
                )
                _dbg(f"Raw Response ({len(raw)} chars)", raw)

            try:
                return _parse_response(raw, available_options)
            except Exception as e:
                last_exc = e
                if debug:
                    _dbg(f"Parse FAILED (attempt {attempt})", str(e))
                    traceback.print_exc()

                if attempt < max_attempts - 1:
                    messages = messages + [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                f"你的输出无法解析为合法 JSON，错误信息：{e}\n"
                                "请只输出符合要求的 JSON，不要包含任何其他内容。"
                            ),
                        },
                    ]

        raise last_exc

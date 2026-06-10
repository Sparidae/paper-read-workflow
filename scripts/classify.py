# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""LLM-based paper classification (type, research areas, institutions)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import llm_call, load_config, load_prompt, output_error, output_ok

_DEFAULT_SYSTEM_TEMPLATE = """你是一位学术论文分类助手，负责为论文打标签。

你需要根据论文标题和摘要，为论文选择分类标签。每个字段都有现有的候选选项。

规则：
1. **优先复用现有选项**：尽量从候选列表中选择最匹配的，保持标签体系一致
2. **允许创建新选项**：如果现有选项确实无法覆盖，可以提出新选项
3. **新选项风格要求**：
   - 论文类型：简短英文词（1-2个词），如 "Survey"、"Framework"
   - 研究领域：英文关键词或缩写（1-2个词），如 "RLHF"、"VisionLM"
   - 来源机构：机构名称，如 "Meta"、"DeepMind"、"CMU"
   - 来源机构名称不要包含英文逗号
4. 每个字段返回 selected（从现有选项选）和 new（需要新建的选项）两个列表
5. selected 中的值必须与现有选项完全一致（包括大小写）

现有候选选项：
- 论文类型（paper_type）：{paper_type_opts}
- 研究领域（research_areas）：{research_areas_opts}
- 来源机构（institutions）：{institutions_opts}

输出必须是合法的 JSON，结构如下：
{{
  "paper_type": {{"selected": [...], "new": [...]}},
  "research_areas": {{"selected": [...], "new": [...]}},
  "institutions": {{"selected": [...], "new": [...]}}
}}

不要输出任何 JSON 以外的内容。"""


def _fmt_options(opts: list[str]) -> str:
    return "、".join(f'"{o}"' for o in opts) if opts else "（暂无）"


def _build_system_prompt(options: dict[str, list[str]]) -> str:
    config = load_config()
    custom_prompt = config.get("llm", {}).get("classifier_prompt")
    template = None
    if custom_prompt:
        try:
            template = load_prompt(custom_prompt)
        except SystemExit:
            template = None

    if template:
        options_block = f"""
现有候选选项：
- 论文类型（paper_type）：{_fmt_options(options.get("paper_type", []))}
- 研究领域（research_areas）：{_fmt_options(options.get("research_areas", []))}
- 来源机构（institutions）：{_fmt_options(options.get("institutions", []))}"""
        return template + options_block

    return _DEFAULT_SYSTEM_TEMPLATE.format(
        paper_type_opts=_fmt_options(options.get("paper_type", [])),
        research_areas_opts=_fmt_options(options.get("research_areas", [])),
        institutions_opts=_fmt_options(options.get("institutions", [])),
    )


def _parse_response(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("Could not parse classifier response")
        data = json.loads(match.group())

    result = {}
    for key in ("paper_type", "research_areas", "institutions"):
        field = data.get(key, {})
        if isinstance(field, dict):
            result[key] = field.get("selected", []) + field.get("new", [])
        elif isinstance(field, list):
            result[key] = field
        else:
            result[key] = []
    return result


def main():
    parser = argparse.ArgumentParser(description="Classify paper via LLM")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument("--model", help="LLM model override")
    parser.add_argument(
        "--options-json", help="JSON string of available Notion options"
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    metadata_path = paper_dir / "metadata.json"
    if not metadata_path.exists():
        output_error("metadata.json not found in paper directory")
        return

    metadata = json.loads(metadata_path.read_text())

    options = {"paper_type": [], "research_areas": [], "institutions": []}
    if args.options_json:
        try:
            options = json.loads(args.options_json)
        except json.JSONDecodeError:
            pass

    system_prompt = _build_system_prompt(options)
    authors_str = ", ".join(metadata.get("authors", []))
    user_prompt = f"""请对以下论文进行分类：

**标题**: {metadata["title"]}
**作者**: {authors_str}
**摘要**: {metadata.get("abstract", "")}"""

    config = load_config()
    llm_config = config.get("llm", {})
    model = args.model or llm_config.get("model")
    max_tokens = llm_config.get("classifier_max_tokens", 8000)

    try:
        raw = llm_call(system_prompt, user_prompt, model=model, max_tokens=max_tokens)
        classification = _parse_response(raw)
    except Exception as e:
        output_error(f"Classification failed: {e}")
        return

    output_path = paper_dir / "classification.json"
    output_path.write_text(json.dumps(classification, ensure_ascii=False, indent=2))

    output_ok(
        f"Classified: {classification.get('paper_type', [])}",
        classification=classification,
        outputs={"classification": str(output_path)},
    )


if __name__ == "__main__":
    main()

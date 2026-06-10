# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""LLM-based one-sentence paper summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import llm_call, load_config, output_error, output_ok

_DEFAULT_SYSTEM_PROMPT = """你是一位精通学术写作的计算机科学专家，擅长将复杂的论文摘要浓缩为信息密度极高的一句话。

Task: 请根据我提供的论文标题和摘要，总结出"一句话摘要"。

Constraints (严苛约束):
- 内容忠实性：所有定语和核心概念必须严格提取自原文，严禁引入外部术语或过度解读。
- 语法结构：使用"通过[手段/方法A]与[手段/方法B]，[实现了核心结论]"的结构。
- 信息密度：尽量将论文中提到的关键机制转化为名词短语嵌入到句子中，确保一句话涵盖"问题-方法-机制-结果"全过程。
- 语言要求：学术、专业、精炼，拒绝口语化表达。

直接输出一句话，不要包含任何前缀、解释或换行。"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate one-sentence paper summary via LLM"
    )
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument("--model", help="LLM model override")
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    metadata_path = paper_dir / "metadata.json"
    if not metadata_path.exists():
        output_error("metadata.json not found in paper directory")
        return

    metadata = json.loads(metadata_path.read_text())

    config = load_config()
    llm_config = config.get("llm", {})
    model = args.model or llm_config.get("model")
    max_tokens = llm_config.get("summarizer_max_tokens", 500)

    system_prompt = _DEFAULT_SYSTEM_PROMPT
    custom_prompt_path = llm_config.get("summarizer_prompt")
    if custom_prompt_path:
        p = Path(custom_prompt_path)
        if p.exists():
            system_prompt = p.read_text(encoding="utf-8").strip()

    user_prompt = (
        f"**标题**: {metadata['title']}\n\n**摘要**:\n{metadata.get('abstract', '')}"
    )

    try:
        summary = llm_call(
            system_prompt, user_prompt, model=model, max_tokens=max_tokens
        )
    except Exception as e:
        output_error(f"Summarization failed: {e}")
        return

    output_path = paper_dir / "summary.txt"
    output_path.write_text(summary, encoding="utf-8")

    output_ok(
        f"Summary: {summary[:80]}...",
        summary=summary,
        outputs={"summary": str(output_path)},
    )


if __name__ == "__main__":
    main()

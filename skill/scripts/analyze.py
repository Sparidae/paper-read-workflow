# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""LLM-based paper analysis — generates full reading notes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import llm_call, load_config, output_error, output_ok

_JSON_SYSTEM_PROMPT = """你是一位专业的学术论文分析助手，擅长深度理解和总结计算机科学、人工智能领域的研究论文。

你的任务是分析给定的论文内容，生成结构化的中文阅读笔记。

输出必须是合法的 JSON，结构如下：
{
  "overview": "一段话概括论文的核心内容和主要发现（200-300字）",
  "research_problem": "论文要解决什么问题，研究动机和背景（150-200字）",
  "methodology": "技术方案、模型架构、核心算法的详细描述（200-400字）",
  "contributions": ["贡献点1", "贡献点2", "贡献点3"],
  "experiments": "数据集、基线方法、关键实验结果和指标（150-300字）",
  "limitations": "作者提到的局限性、潜在问题和未来工作方向（100-200字）",
  "key_takeaways": ["要点1", "要点2", "要点3"]
}

要求：
- 所有内容用中文输出
- contributions 列表包含 3-5 个具体贡献点
- key_takeaways 列表包含 3-5 个最重要的结论或洞见
- 不要输出任何 JSON 以外的内容
- 确保 JSON 格式合法"""

_FIGURE_PLACEMENT = """

## 图片插入规则

本论文包含以下图片，请在笔记中与该图片内容最相关的章节末尾，单独一行插入标记 `[FIGURE:N]`（N 为图片编号）。
每张图片只使用一次，标记必须独占一行。

{figures_list}"""

_TABLE_PLACEMENT = """

## 表格插入规则

本论文包含以下表格，请在笔记中与该表格内容最相关的章节末尾，单独一行插入标记 `[TABLE:N]`（N 为表格编号）。
每张表格只使用一次，标记必须独占一行。

{tables_list}"""


def _build_user_prompt(metadata: dict, paper_text: str) -> str:
    authors_str = ", ".join(metadata.get("authors", []))
    return f"""请分析以下论文：

**标题**: {metadata["title"]}
**作者**: {authors_str}
**摘要**: {metadata.get("abstract", "")}

**论文正文（节选）**:
{paper_text}"""


def main():
    parser = argparse.ArgumentParser(description="Generate reading notes via LLM")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument("--model", help="LLM model override")
    parser.add_argument(
        "--format", choices=["json", "freeform"], default=None, help="Note format"
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    metadata_path = paper_dir / "metadata.json"
    text_path = paper_dir / "text.txt"

    if not metadata_path.exists():
        output_error("metadata.json not found")
        return
    if not text_path.exists():
        output_error("text.txt not found — run extract_text first")
        return

    metadata = json.loads(metadata_path.read_text())
    paper_text = text_path.read_text(encoding="utf-8")

    config = load_config()
    llm_config = config.get("llm", {})
    model = args.model or llm_config.get("model")
    note_format = args.format or llm_config.get("note_format", "freeform")
    max_input_tokens = llm_config.get("max_input_tokens", 100000)
    max_output_tokens = llm_config.get("max_output_tokens", 4000)

    max_chars = max_input_tokens * 4 - 8000
    if len(paper_text) > max_chars:
        paper_text = paper_text[:max_chars] + "\n\n[... 文档已截断 ...]"

    if note_format == "json":
        system_prompt = _JSON_SYSTEM_PROMPT
    else:
        custom_prompt_path = llm_config.get("analyzer_prompt")
        if custom_prompt_path:
            p = Path(custom_prompt_path)
            if p.exists():
                system_prompt = p.read_text(encoding="utf-8").strip()
            else:
                system_prompt = _JSON_SYSTEM_PROMPT
                note_format = "json"
        else:
            from _lib import find_project_root

            prompt_path = find_project_root() / "prompts" / "analyzer.md"
            if prompt_path.exists():
                system_prompt = prompt_path.read_text(encoding="utf-8").strip()
            else:
                system_prompt = _JSON_SYSTEM_PROMPT
                note_format = "json"

    if note_format == "freeform":
        visuals_path = paper_dir / "visuals.json"
        if visuals_path.exists():
            visuals = json.loads(visuals_path.read_text())
            figures = [v for v in visuals if v.get("kind") == "figure"]
            tables = [v for v in visuals if v.get("kind") == "table"]
            if figures:
                figures_list = "\n".join(
                    f"- 图片 {f['number']}：{f.get('caption', '（无说明）')}"
                    for f in figures
                )
                system_prompt += _FIGURE_PLACEMENT.format(figures_list=figures_list)
            if tables:
                tables_list = "\n".join(
                    f"- 表格 {t['number']}：{t.get('caption', '（无说明）')}"
                    for t in tables
                )
                system_prompt += _TABLE_PLACEMENT.format(tables_list=tables_list)

    user_prompt = _build_user_prompt(metadata, paper_text)

    try:
        raw = llm_call(
            system_prompt, user_prompt, model=model, max_tokens=max_output_tokens
        )
    except Exception as e:
        output_error(f"Analysis failed: {e}")
        return

    if note_format == "freeform":
        source = metadata.get("source", "")
        paper_id = metadata.get("paper_id", "")
        if source == "Arxiv" and paper_id:
            alphaxiv_url = f"https://alphaxiv.org/abs/{paper_id}"
            raw = f"[🔖 alphaXiv · {paper_id}]({alphaxiv_url})\n\n" + raw

        output_path = paper_dir / "notes.md"
        output_path.write_text(raw, encoding="utf-8")
    else:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                output_error("Failed to parse LLM response as JSON")
                return

        output_path = paper_dir / "notes.json"
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    output_ok(
        f"Generated {note_format} notes ({len(raw)} chars)",
        format=note_format,
        char_count=len(raw),
        outputs={"notes": str(output_path)},
    )


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""LLM-based figure/table caption translation to Chinese."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import llm_call, load_config, output_error, output_ok

_SYSTEM_PROMPT = """你是一位学术论文翻译专家。将给定的图片说明（figure caption）翻译成中文。

要求：
1. 保留数学公式、变量名、模型名称原样不变
2. 专业术语可保留英文并附中文注释，如 attention（注意力）
3. 翻译要准确、流畅、符合学术规范
4. 以 JSON 数组返回，元素顺序与输入完全一致，只输出 JSON 不要有其他内容"""


def _strip_json_fence(raw: str) -> str:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def main():
    parser = argparse.ArgumentParser(
        description="Translate figure/table captions via LLM"
    )
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument("--model", help="LLM model override")
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    visuals_path = paper_dir / "visuals.json"
    if not visuals_path.exists():
        output_error("visuals.json not found — run extract_visuals first")
        return

    visuals = json.loads(visuals_path.read_text())
    captions_to_translate = [
        (i, v["caption"]) for i, v in enumerate(visuals) if v.get("caption", "").strip()
    ]

    if not captions_to_translate:
        output_path = paper_dir / "captions.json"
        output_path.write_text("[]")
        output_ok(
            "No captions to translate",
            translated_count=0,
            outputs={"captions": str(output_path)},
        )
        return

    config = load_config()
    llm_config = config.get("llm", {})
    model = args.model or llm_config.get("model")
    max_tokens = llm_config.get("translator_max_tokens", 8000)

    captions_input = json.dumps(
        [cap for _, cap in captions_to_translate], ensure_ascii=False
    )
    user_prompt = f"请翻译以下图片说明：\n{captions_input}"

    try:
        raw = llm_call(_SYSTEM_PROMPT, user_prompt, model=model, max_tokens=max_tokens)
        translated = json.loads(_strip_json_fence(raw))
        if not isinstance(translated, list) or len(translated) != len(
            captions_to_translate
        ):
            raise ValueError(
                f"Expected {len(captions_to_translate)} translations, got {len(translated) if isinstance(translated, list) else 'non-list'}"
            )
    except Exception as e:
        output_error(f"Caption translation failed: {e}")
        return

    captions_data = []
    for (idx, original), new_caption in zip(captions_to_translate, translated):
        captions_data.append(
            {
                "index": idx,
                "original": original,
                "translated": new_caption if isinstance(new_caption, str) else original,
                "kind": visuals[idx].get("kind", "figure"),
                "number": visuals[idx].get("number", 0),
            }
        )

    output_path = paper_dir / "captions.json"
    output_path.write_text(json.dumps(captions_data, ensure_ascii=False, indent=2))

    output_ok(
        f"Translated {len(captions_data)} captions",
        translated_count=len(captions_data),
        outputs={"captions": str(output_path)},
    )


if __name__ == "__main__":
    main()

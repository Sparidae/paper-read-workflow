# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.0.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""Shared utilities for paper-reading-workflow scripts."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class FigureInfo:
    """A single figure or table extracted from a paper's LaTeX source."""

    image_path: Path
    caption: str
    label: str = ""
    number: int = 0
    kind: str = "figure"
    render_backend: str = ""


def find_project_root() -> Path:
    """Walk up from cwd looking for config.yaml or .env."""
    candidate = Path.cwd()
    for _ in range(10):
        if (candidate / "config.yaml").exists() or (candidate / ".env").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return Path.cwd()


def load_config() -> dict[str, Any]:
    """Load .env and config.yaml, return merged config dict."""
    import yaml
    from dotenv import load_dotenv

    root = find_project_root()

    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    config_path = root / "config.yaml"
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def papers_dir(config: dict[str, Any] | None = None) -> Path:
    """Resolve papers storage directory."""
    if config is None:
        config = load_config()
    raw = config.get("storage", {}).get("papers_dir", "papers")
    path = Path(raw)
    if not path.is_absolute():
        path = find_project_root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_llm_client(use_vision: bool = False):
    """Create an OpenAI-compatible client from env vars."""
    from openai import OpenAI

    if use_vision:
        api_key = os.getenv("OPENAI_VISION_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def llm_call(
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    use_vision: bool = False,
) -> str:
    """Simple blocking LLM completion. Returns response text."""
    config = load_config()
    llm_config = config.get("llm", {})

    client = make_llm_client(use_vision=use_vision)
    effective_model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
    effective_temp = (
        temperature if temperature is not None else llm_config.get("temperature", 0.2)
    )

    kwargs: dict[str, Any] = {
        "model": effective_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": effective_temp,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    response = client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = getattr(choice.message, "content", None) or ""
    if not content.strip():
        content = getattr(choice.message, "reasoning_content", None) or ""
    return content.strip()


def load_prompt(name: str) -> str:
    """Load a prompt template from skill/assets/prompts/."""
    root = find_project_root()
    path = root / "skill" / "assets" / "prompts" / name
    if not path.exists():
        output_error(f"Prompt file not found: {path}")
        sys.exit(1)
    return path.read_text(encoding="utf-8").strip()


def output_json(status: str, message: str, **extra) -> None:
    """Print structured JSON to stdout and exit with appropriate code."""
    result = {"status": status, "message": message, **extra}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if status == "error":
        sys.exit(1)


def output_error(message: str, **extra) -> None:
    """Print error JSON and exit 1."""
    output_json("error", message, **extra)


def output_ok(message: str, **extra) -> None:
    """Print success JSON and exit 0."""
    output_json("ok", message, **extra)

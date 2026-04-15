"""Configuration loader: merges config.yaml with .env environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = two levels up from this file (src/paper_tool/config.py)
PROJECT_ROOT = Path(__file__).parent.parent.parent
NOTION_SCHEMA_PATH = PROJECT_ROOT / "notion_schema.yaml"


def _find_env_file() -> Path | None:
    """Search for .env file starting from the current working directory upward."""
    candidate = Path.cwd()
    for _ in range(5):
        env_path = candidate / ".env"
        if env_path.exists():
            return env_path
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    # Fallback to project root
    fallback = PROJECT_ROOT / ".env"
    return fallback if fallback.exists() else None


def _find_config_file() -> Path | None:
    """
    Search for config.yaml starting from the current working directory.
    Falls back to config.yaml.example if no local config.yaml exists.
    """
    candidate = Path.cwd()
    for _ in range(5):
        config_path = candidate / "config.yaml"
        if config_path.exists():
            return config_path
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    # Fallback: project root config.yaml, then config.yaml.example
    for name in ("config.yaml", "config.yaml.example"):
        fallback = PROJECT_ROOT / name
        if fallback.exists():
            return fallback
    return None


class Config:
    """Centralized configuration object."""

    def __init__(self) -> None:
        env_file = _find_env_file()
        if env_file:
            load_dotenv(env_file)

        config_file = _find_config_file()
        if config_file:
            with open(config_file) as f:
                self._yaml: dict[str, Any] = yaml.safe_load(f) or {}
        else:
            self._yaml = {}

        if NOTION_SCHEMA_PATH.exists():
            with open(NOTION_SCHEMA_PATH) as f:
                self._notion_schema_yaml: dict[str, Any] = yaml.safe_load(f) or {}
        else:
            self._notion_schema_yaml = {}

        self._config_dir = config_file.parent if config_file else PROJECT_ROOT

    def _notion_config(self) -> dict[str, Any]:
        return dict(self._notion_schema_yaml.get("notion", {}))

    # ── Notion ──────────────────────────────────────────────────────────────

    @property
    def notion_token(self) -> str:
        token = os.getenv("NOTION_TOKEN", "")
        if not token:
            raise ValueError(
                "NOTION_TOKEN is not set. Please add it to your .env file."
            )
        return token

    @property
    def notion_database_id(self) -> str:
        db_id = os.getenv("NOTION_DATABASE_ID", "")
        if not db_id:
            raise ValueError(
                "NOTION_DATABASE_ID is not set. Please add it to your .env file."
            )
        return db_id

    @property
    def notion_parent_page_id(self) -> str:
        return os.getenv("NOTION_PARENT_PAGE_ID", "").strip()

    @property
    def notion_properties(self) -> dict[str, str]:
        notion = self._notion_config()
        return notion.get(
            "properties",
            {
                "title": "论文笔记",
                "authors": "作者",
                "abstract": "一句话摘要",
                "source": "来源",
                "importance": "重要性",
                "citation_count": "引用量",
                "url": "论文链接",
                "published_date": "发表日期",
                "added_date": "添加日期",
                "created_time": "创建时间",
                "last_edited_time": "上次编辑时间",
                "tags": "研究领域",
                "paper_type": "论文类型",
                "institution": "来源机构",
                "status": "阅读状态",
            },
        )

    @property
    def notion_paper_type_prop(self) -> str:
        return self._notion_config().get("properties", {}).get("paper_type", "")

    @property
    def notion_institution_prop(self) -> str:
        return self._notion_config().get("properties", {}).get("institution", "")

    @property
    def notion_status_type(self) -> str:
        """'select' or 'checkbox'"""
        return self._notion_config().get("status_type", "checkbox")

    @property
    def notion_default_status(self) -> str:
        return self._notion_config().get("default_status", "Unread")

    @property
    def notion_database_title(self) -> str:
        return self._notion_config().get("database_title", "paper-tool Papers")

    @property
    def citations_refresh_interval_days(self) -> int:
        raw = self._yaml.get("citations", {}).get("refresh_interval_days", 7)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 7

    # ── LLM ─────────────────────────────────────────────────────────────────

    @property
    def llm_model(self) -> str:
        return self._yaml.get("llm", {}).get("model", "openai/gpt-4o")

    def _load_prompt(self, key: str) -> str | None:
        """
        Load a prompt file specified by config key.
        Returns None if not configured or missing.
        """
        rel_path = self._yaml.get("llm", {}).get(key)
        if not rel_path:
            return None
        path = Path(rel_path)
        if not path.is_absolute():
            path = self._config_dir / path
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return None

    @property
    def llm_note_format(self) -> str:
        """'json' or 'freeform'"""
        return self._yaml.get("llm", {}).get("note_format", "json")

    @property
    def analyzer_prompt(self) -> str | None:
        return self._load_prompt("analyzer_prompt")

    @property
    def classifier_prompt(self) -> str | None:
        return self._load_prompt("classifier_prompt")

    @property
    def summarizer_prompt(self) -> str | None:
        return self._load_prompt("summarizer_prompt")

    @property
    def llm_summarizer_max_tokens(self) -> int:
        return self._yaml.get("llm", {}).get("summarizer_max_tokens", 500)

    @property
    def llm_max_input_tokens(self) -> int:
        return self._yaml.get("llm", {}).get("max_input_tokens", 100000)

    @property
    def llm_max_output_tokens(self) -> int:
        return self._yaml.get("llm", {}).get("max_output_tokens", 4000)

    @property
    def llm_classifier_max_tokens(self) -> int:
        """Max tokens for classifier response. Thinking models need more budget."""
        return self._yaml.get("llm", {}).get("classifier_max_tokens", 8000)

    @property
    def llm_translator_max_tokens(self) -> int:
        """
        Max tokens for figure caption translation.
        Thinking models need more budget.
        """
        return self._yaml.get("llm", {}).get("translator_max_tokens", 8000)

    @property
    def max_figures(self) -> int:
        """Maximum number of figures to extract and upload per paper."""
        return self._yaml.get("llm", {}).get("max_figures", 15)

    @property
    def rerender_figures(self) -> bool:
        """Whether to ignore cached rendered figure PNGs and rerender them."""
        return bool(self._yaml.get("llm", {}).get("rerender_figures", False))

    @property
    def max_tables(self) -> int:
        """Maximum number of tables to extract and upload per paper."""
        return self._yaml.get("llm", {}).get("max_tables", 10)

    @property
    def rerender_tables(self) -> bool:
        """Whether to ignore cached table PNGs and rerender them."""
        return bool(self._yaml.get("llm", {}).get("rerender_tables", False))

    @property
    def llm_temperature(self) -> float:
        return self._yaml.get("llm", {}).get("temperature", 0.2)

    @property
    def llm_stream_window(self) -> bool:
        """Whether to render LLM token streaming output."""
        return bool(self._yaml.get("llm", {}).get("stream_window", False))

    @property
    def llm_stream_window_height(self) -> int:
        """Legacy stream render height setting (kept for backward compatibility)."""
        raw = self._yaml.get("llm", {}).get("stream_window_height", 8)
        try:
            return max(4, int(raw))
        except (TypeError, ValueError):
            return 8

    # ── Storage ──────────────────────────────────────────────────────────────

    @property
    def papers_dir(self) -> Path:
        raw = self._yaml.get("storage", {}).get("papers_dir", "papers")
        path = Path(raw)
        if not path.is_absolute():
            path = self._config_dir / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── OpenAI-compatible endpoint ───────────────────────────────────────────

    @property
    def openai_base_url(self) -> str | None:
        """Custom OpenAI-compatible base URL (e.g. DeepSeek, Kimi, vLLM, etc.)."""
        return os.getenv("OPENAI_BASE_URL") or None

    # ── OpenReview ───────────────────────────────────────────────────────────

    @property
    def openreview_username(self) -> str:
        return os.getenv("OPENREVIEW_USERNAME", "")

    @property
    def openreview_password(self) -> str:
        return os.getenv("OPENREVIEW_PASSWORD", "")

    def show(self) -> None:
        """Print current configuration (masks sensitive values)."""
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="paper-tool 当前配置", show_lines=True)
        table.add_column("配置项", style="cyan")
        table.add_column("值", style="green")

        def mask(val: str) -> str:
            return val[:8] + "..." if len(val) > 8 else ("(未设置)" if not val else val)

        table.add_row("LLM 模型", self.llm_model)
        table.add_row("最大输入 Token", str(self.llm_max_input_tokens))
        table.add_row("最大输出 Token", str(self.llm_max_output_tokens))
        table.add_row("LLM 流式输出", "开启" if self.llm_stream_window else "关闭")
        table.add_row("流式高度(兼容项)", str(self.llm_stream_window_height))
        table.add_row("PDF 存储目录", str(self.papers_dir))
        table.add_row("Notion Token", mask(os.getenv("NOTION_TOKEN", "")))
        table.add_row("Notion Database ID", mask(os.getenv("NOTION_DATABASE_ID", "")))
        table.add_row(
            "Notion Parent Page ID", mask(os.getenv("NOTION_PARENT_PAGE_ID", ""))
        )
        table.add_row("Notion Status Type", self.notion_status_type)
        table.add_row("引用量刷新间隔(天)", str(self.citations_refresh_interval_days))
        table.add_row("OpenAI Key", mask(os.getenv("OPENAI_API_KEY", "")))
        table.add_row(
            "OpenAI Base URL", os.getenv("OPENAI_BASE_URL", "") or "(官方默认)"
        )
        table.add_row("Anthropic Key", mask(os.getenv("ANTHROPIC_API_KEY", "")))
        table.add_row("Gemini Key", mask(os.getenv("GEMINI_API_KEY", "")))

        console.print(table)


# Singleton
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Package Manager

Use `uv` exclusively. Install dependencies with `uv sync`. Run the CLI with `uv run paper-tool <command>`.

## Key Commands

```bash
uv run paper-tool add <url>            # Add a single paper (PDF download + LLM analysis + Notion write)
uv run paper-tool batch <file>         # Batch add from a URL list file (# comments supported)
uv run paper-tool chat <identifier>    # Interactive multi-turn Q&A with a paper
uv run paper-tool config init          # Interactive .env setup wizard
uv run paper-tool config show          # Show current config (masks secrets)
uv run paper-tool config check-db      # Verify Notion database schema
```

Important flags: `--skip-llm` (metadata only), `--debug` (print raw LLM prompts/responses), `--stream` (streaming output window), `--force` (override duplicate after archiving old page).

## Configuration

Runtime config: `config.yaml` (model, token budgets, note format, prompt paths).
Secrets: `.env` (NOTION_TOKEN, NOTION_DATABASE_ID, plus at least one LLM API key).

The config loader searches upward from cwd, then falls back to the project root.

Note format can be `"json"` (structured parsing) or `"freeform"` (raw LLM output). Changing this affects how `notion_service.py` writes properties.

Token budgets (`max_input_tokens`, `max_output_tokens`, `classifier_max_tokens`, `translator_max_tokens`) are set per-task in `config.yaml`. Thinking/reasoning models need larger `classifier_max_tokens`.

## LLM Integration

Uses `litellm` for provider abstraction. Switch models by editing `config.yaml` `llm.model` only — no code changes needed. Prompts are loaded from `prompts/` (customizable markdown files).

## Duplicate Detection

Before processing, the tool checks Notion for an existing page with the same URL. Re-importing requires `--force`, which archives the old page first.

## Branch & PR Conventions

- Create feature branches off `main` for all changes.
- Open PRs targeting `main`.
- Working branch `master` is legacy; do not push directly to `master`.

## Commit Message Format

使用 `[type]subject` 格式，中文描述：

- `[feat]` 新功能
- `[fix]` 问题修复
- `[refactor]` 重构
- `[doc]` 文档
- `[chore]` 构建/依赖/配置等杂项
- `[perf]` 性能优化
- `[style]` 格式调整（不影响逻辑）

示例：`[feat]支持批量导入` / `[fix]修复arxiv ID解析错误`

## Code Style

Formatted with ruff (line-length 88, Python 3.12). Run `uv run ruff format src/` and `uv run ruff check --fix src/` before committing.

## No Tests

There is no test suite. Verify changes manually using `--debug` flag and a real paper URL against a test Notion database.

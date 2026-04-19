# CLAUDE.md

## Package Manager

Use `uv` exclusively. Never use `pip` or `poetry`. Install with `uv sync`. Run CLI with `uv run paper-tool <command>`.

## Key Commands

```bash
uv run paper-tool add <url>            # PDF + LLM analysis + Notion write
uv run paper-tool batch <file>         # Batch add from URL list (# comments supported)
uv run paper-tool chat <identifier>    # Multi-turn Q&A with a paper
uv run paper-tool config init          # Interactive .env setup wizard
uv run paper-tool config show          # Show current config (masks secrets)
uv run paper-tool config check-db      # Verify Notion database schema
```

Key flags: `--skip-llm`, `--debug` (print raw prompts/responses), `--stream`, `--force` (archive old page then re-import).

## Configuration

- `config.yaml`: runtime config (model, token budgets, note format, prompt paths)
- `.env`: secrets (NOTION_TOKEN, NOTION_DATABASE_ID, LLM API keys)
- Config loader searches **upward from cwd**, then falls back to project root.
- `note_format: "json"` vs `"freeform"` — affects how `notion_service.py` writes Notion properties.
- Thinking/reasoning models need a larger `classifier_max_tokens` in `config.yaml`.

## Branch & PR

- Feature branches off `main`; PRs target `main`.
- `master` is legacy — do not push to it.

## Commit Message Format

`[type]中文描述` — types: `feat` / `fix` / `refactor` / `doc` / `chore` / `perf` / `style`

Example: `[fix]修复arxiv ID解析错误`

## Code Style

Ruff, line-length 88, Python 3.12. Before committing:

```bash
uv run ruff format src/
uv run ruff check --fix src/
```

## Table Rendering Compatibility

- Table image rendering must prioritize maximum compatibility across papers.
- Any new table parsing support or parser bug fix must be backward-compatible and must not break previously working table extraction/rendering behavior.
- When extending table parsing logic, preserve the existing fallback chain and existing successful cases unless there is a deliberate, verified compatibility improvement.
- For table-related changes, verify not only the target paper but also that existing papers with working tables do not regress.

## No Tests

No test suite. Verify with `uv run paper-tool add --debug <url>` against a test Notion database.

---
name: paper-reading-workflow
description: Use when the user wants to add papers to Notion, process arxiv/OpenReview papers, generate AI reading notes, debug LaTeX figure/table rendering failures, batch import papers, or manage their academic paper reading pipeline. Triggers on mentions of 论文, paper reading, arxiv, Notion笔记, LaTeX渲染, 表格渲染, 图表提取, paper-tool, or any paper processing workflow.
---

# Paper Reading Workflow

## Overview

Automated end-to-end pipeline: paper URL → PDF/LaTeX download → text extraction → figure/table rendering → LLM analysis → Notion reading notes. Built on the `paper-tool` CLI, this skill guides usage, debugging, and script composition.

**Core principle:** The CLI handles the happy path. This skill handles everything else — debugging LaTeX failures, composing reusable scripts, and knowing where things live.

## When to Use

- Adding single or batch papers to a Notion reading database
- LaTeX figure/table rendering fails (blank images, missing macros, fallback artifacts)
- Setting up the pipeline on a new machine
- Refreshing citation counts across the Notion database
- Interactive Q&A with a paper that's already been downloaded
- Re-importing a paper after fixing parser bugs

Skip this skill for: reading PDFs directly, general LaTeX questions, Notion API usage outside paper processing.

## Prerequisites

The CLI tool must be installed and configured before any workflow runs:

```bash
cd <project_root>
uv sync
```

**Secrets check** — run `uv run paper-tool config show` and verify:
- `NOTION_TOKEN`, `NOTION_DATABASE_ID` are set
- `OPENAI_MODEL`, `OPENAI_API_KEY` are set
- `OPENAI_BASE_URL` points to the correct endpoint

**Database check** — ensure the Notion database has the expected schema:
```bash
uv run paper-tool config check-db
```

If the database doesn't exist yet, create it:
```bash
uv run python scripts/create_notion_db.py --parent-page-id <32-char-hex-id>
```

## Quick Reference

| Task | Command |
|------|---------|
| Add a paper | `uv run paper-tool add <url>` |
| Add with debug output | `uv run paper-tool add --debug <url>` |
| Force re-import | `uv run paper-tool add --force <url>` |
| Skip LLM (download only) | `uv run paper-tool add --skip-llm <url>` |
| Batch from file | `uv run paper-tool batch <file>` |
| Chat with a paper | `uv run paper-tool chat <arxiv_id>` |
| Refresh citations | `uv run paper-tool refresh-citations` |
| Show config | `uv run paper-tool config show` |
| Interactive setup | `uv run paper-tool config init` |
| Start web UI | `uv run paper-tool serve` |

Key flags: `--debug` prints raw prompts and LLM responses. `--stream` shows token-by-token output. `--force` archives existing Notion page and re-imports.

## Daily Workflows

### Adding a Single Paper

```
User: "帮我读这篇论文 https://arxiv.org/abs/2301.12345"
```

1. Run `uv run paper-tool add <url>` and watch the progress
2. If it succeeds, share the Notion page URL
3. If LaTeX rendering fails, follow the [LaTeX Debugging](#latex-debugging-workflow) workflow below

### Batch Import

```
User: "把这个 Markdown 文件里的所有论文链接都导入"
```

1. Extract URLs from the file if needed, or point the tool at it directly
2. Run `uv run paper-tool batch <file> --continue-on-error`
3. `--continue-on-error` keeps going past failures — useful for large batches
4. Re-run individual failures with `--debug` to diagnose

### Chat with a Paper

```
User: "我想和 ResNet 那篇论文聊一下"
```

1. Find the paper: `uv run paper-tool chat <identifier>` where identifier can be an arxiv ID or a partial directory name
2. The chat session loads the full paper text as context
3. Use `/reset` to clear conversation history, `/exit` to quit

### Refresh Citations

```
User: "帮我更新一下所有论文的引用数"
```

Run `uv run paper-tool refresh-citations`. This queries Semantic Scholar in batches, respects rate limits, and logs to `.refresh-citation.log`.

## LaTeX Debugging Workflow

This is the most common failure mode. Different papers use different LaTeX packages, custom macros, and non-standard formatting — the parser covers common cases but will fail on edge cases.

### Step 1: Identify the Failure Type

Check the terminal output from `paper-tool add`. The tool reports how each figure/table was rendered:

- `latex` — pdflatex compilation succeeded
- `matplotlib` — fell back to matplotlib table rendering
- `cached` — reused a previous render
- Missing figure — rendering failed silently

For detailed logs, check `papers/<paper_dir>/debug/` — it contains `.tex`, `.log`, `.stdout`, `.stderr`, and `.json` status files for each figure and table.

### Step 2: Isolate and Reproduce

Use the debug scripts to extract and compile a single figure or table in isolation:

```bash
# Render a specific figure from paper.tex in isolation
bash .claude/skills/paper-reading-workflow/scripts/debug-figure.sh \
  papers/<paper_dir>/paper.tex <figure_index> [output_dir]

# Same for tables
bash .claude/skills/paper-reading-workflow/scripts/debug-table.sh \
  papers/<paper_dir>/paper.tex <table_index> [output_dir]
```

This extracts the figure/table body and preamble macros, writes a standalone `.tex` file, compiles it with pdflatex, and opens the resulting PNG. Much faster than re-running the full pipeline.

### Step 3: Diagnose and Fix

Common failure patterns — see `reference/latex-failure-patterns.md` for the full catalog. Quick hits:

- **Missing macro**: `\newcommand` defined in body (not preamble) → inject into preamble
- **`\resizebox` wrapping table**: pdflatex can't handle it → strip wrapper, retry
- **Font package not installed**: map to available fonts or install missing package
- **pgfplotstable with external data**: data file not found → inline the data
- **TikZ figure touches border**: textwidth detection wrong → increase page height

After identifying the fix, modify the relevant parser in `src/paper_tool/` (`figure_extractor.py` or `table_extractor.py`).

### Step 4: Verify the Fix

First, verify the target paper works:
```bash
uv run paper-tool add <paper_url> --force --rerender-figures --rerender-tables
```

Then, verify previously-working papers don't regress. See `reference/latex-failure-patterns.md` for the regression checklist. The project CLAUDE.md has a standing rule: any table/figure parser change must be backward-compatible.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/debug-figure.sh` | Extract and standalone-render a single figure from paper.tex |
| `scripts/debug-table.sh` | Extract and standalone-render a single table from paper.tex |
| `scripts/reimport-paper.sh` | Force re-import a paper by paper_dir name |
| `scripts/create_notion_db.py` | Create a Notion database from `notion_schema.yaml` |
| `scripts/rank_by_likes.py` | Rank papers by alphaXiv likes + S2 citations |

## Configuration Reference

### Key Paths

| Item | Location |
|------|----------|
| Project root | Determined at runtime; usually where `config.yaml` lives |
| Runtime config | `config.yaml` (searches upward from cwd, falls back to project root) |
| Secrets | `.env` (same search strategy as config.yaml) |
| Paper storage | `papers/` (configurable via `storage.papers_dir`) |
| Prompt templates | `prompts/analyzer.md`, `classifier.md`, `summarizer.md` |
| Notion schema | `notion_schema.yaml` |
| Debug artifacts | `papers/<paper_dir>/debug/` |
| Logs | `logs/paper_tool.log` (rotating, 10 MB × 5 backups, 30-day retention) |

### Config Toggles

Read `reference/paths.md` for the complete config reference. Key operational toggles:

- `llm.note_format: "freeform"` — raw Markdown output (current default), `"json"` for structured
- `llm.max_figures: 15` / `llm.max_tables: 10` — cap images per paper
- `llm.rerender_figures: true` / `llm.rerender_tables: true` — force re-render (turn off for speed)
- `notion.status_type: "checkbox"` — how the Status property is stored

## Project Structure

When investigating parser bugs, you'll work in these source files:

```
src/paper_tool/
├── figure_extractor.py   # Figure parsing + pdflatex rendering
├── table_extractor.py    # Table parsing + pdflatex/matplotlib rendering
├── pdf_parser.py         # PDF + LaTeX text extraction
├── downloaders/arxiv.py  # Arxiv metadata, PDF, LaTeX source download
├── pipeline.py           # Orchestration (event-driven)
├── notion_service.py     # Notion API (pages, blocks, uploads)
└── llm_*.py              # LLM analyzer, classifier, summarizer, chat, stream
```

## Common Pitfalls

- **Config not found**: `config.yaml` searches upward from cwd. Run commands from within the project or a subdirectory.
- **Notion 404**: The database ID in `.env` may be wrong. Run `paper-tool config check-db` to verify.
- **Thinking model token exhaustion**: Models like kimi-k2.5 consume many tokens in CoT. Increase `classifier_max_tokens` and `translator_max_tokens` in `config.yaml`.
- **`\include` not resolved**: `_expand_tex_includes` may miss non-standard paths. Check the merged `paper.tex`.
- **Figure rendered as blank PNG**: The image file in `figures/` may be PDF-format. The tool auto-converts PDF to PNG, but check `convert_pdf_figures()` didn't fail.

---
name: paper-reading-workflow
description: Use when the user wants to add papers to Notion, process arxiv/OpenReview papers, generate AI reading notes, debug LaTeX figure/table rendering failures, batch import papers, or manage their academic paper reading pipeline. Triggers on mentions of 论文, paper reading, arxiv, Notion笔记, LaTeX渲染, 表格渲染, 图表提取, paper-tool, or any paper processing workflow.
---

# Paper Reading Workflow

## Overview

This is an agent-oriented toolkit for processing academic papers. Every pipeline step is a Python function you can import and call directly — no CLI subprocess needed. The `PipelineContext` dataclass bundles all configuration; construct it once and pass it to whichever steps you need.

**Core principle:** You have full control. Mix and match steps, skip the ones you don't need, inject your own logic between them.

## Setup

Always start by loading configuration:

```python
from paper_tool.config import PipelineContext

ctx = PipelineContext.from_config()
```

This reads `config.yaml` and `.env` with the same search logic as the CLI. Every business class also has a `from_context(ctx)` factory, or you can pass parameters explicitly:

```python
from paper_tool.llm_analyzer import LLMAnalyzer

# Option A: from config
analyzer = LLMAnalyzer.from_context(ctx)

# Option B: explicit (agent-controlled)
analyzer = LLMAnalyzer(
    model="gpt-4o",
    max_input_tokens=100000,
    max_output_tokens=4000,
    temperature=0.2,
)
```

## Core Workflow: Adding a Single Paper

The full pipeline is available as `run_pipeline()` for one-shot use, but agents should compose steps directly for more control:

```python
from paper_tool.config import PipelineContext
from paper_tool.pipeline import download_paper, extract_paper_text
from paper_tool.llm_classifier import LLMClassifier
from paper_tool.llm_summarizer import LLMSummarizer
from paper_tool.llm_analyzer import LLMAnalyzer, translate_captions
from paper_tool.notion_service import NotionService

ctx = PipelineContext.from_config()

# Step 1: Download
result = download_paper("https://arxiv.org/abs/2301.12345", ctx.papers_dir)
# result.downloader, result.metadata, result.pdf_path

# Step 2: Extract text + visuals
ext = extract_paper_text(
    result.downloader, result.metadata, result.pdf_path, ctx.papers_dir,
    max_input_tokens=ctx.llm_max_input_tokens,
    max_figures=ctx.max_figures,
    max_tables=ctx.max_tables,
    rerender_figures=ctx.rerender_figures,
    rerender_tables=ctx.rerender_tables,
)
# ext.paper_text, ext.tex_path, ext.figures, ext.tables

# Step 3: Notion
notion = NotionService.from_context(ctx)
page_id = notion.create_page(result.metadata)

# Step 4: Classify
classifier = LLMClassifier.from_context(ctx)
options = notion.get_classification_options()
classification = classifier.classify(result.metadata, options)
notion.update_classifications(page_id, classification)

# Step 5: Summarize
summarizer = LLMSummarizer.from_context(ctx)
summary = summarizer.summarize(result.metadata)
notion.update_summary(page_id, summary)

# Step 6: Translate captions
if ext.figures:
    ext.figures = translate_captions(ext.figures, model=ctx.llm_model,
                                      temperature=ctx.llm_temperature,
                                      translator_max_tokens=ctx.llm_translator_max_tokens)

# Step 7: Analyze
analyzer = LLMAnalyzer.from_context(ctx)
note = analyzer.analyze(result.metadata, ext.paper_text,
                        figures=ext.figures, tables=ext.tables)

# Step 8: Write to Notion
notion.append_note_with_figures(page_id, note, ext.figures, ext.tables)
```

### Shortcut: run_pipeline()

For simple cases, `run_pipeline()` still works. It internally uses the same step functions:

```python
from paper_tool.pipeline import run_pipeline

success = run_pipeline("https://arxiv.org/abs/2301.12345")
```

## Batch Import

Iterate over URLs and call the steps. Use `--continue-on-error` semantics by catching exceptions per paper:

```python
from paper_tool.pipeline import download_paper, extract_paper_text

urls = ["https://arxiv.org/abs/2301.12345", "https://arxiv.org/abs/2301.12346"]
for url in urls:
    try:
        result = download_paper(url, ctx.papers_dir)
        # ... process each paper
    except Exception as e:
        print(f"Failed: {url} — {e}")
        continue
```

## Chat with a Paper

```python
from paper_tool.llm_chat import ChatSession, find_paper_file

file_path = find_paper_file("2301.12345", ctx.papers_dir)
session = ChatSession.from_context(file_path, ctx=ctx)
answer = session.ask("这篇论文的主要贡献是什么？")
```

## LaTeX Debugging Workflow

This is the most common failure mode. Different papers use different LaTeX packages, custom macros, and non-standard formatting.

### Step 1: Identify the Failure Type

Check `papers/<paper_dir>/debug/` — each table/figure has:
- `table_NN.json` / `figure_NN.json` — `renderer` field: `"latex"`, `"matplotlib"`, or `"cached"`
- `.latex.tex`, `.latex.log`, `.latex.stdout.txt`, `.latex.stderr.txt` — compilation artifacts

### Step 2: Isolate and Reproduce

Use the debug scripts to compile a single figure or table in isolation:

```bash
bash .claude/skills/paper-reading-workflow/scripts/debug-table.sh \
  papers/<paper_dir>/paper.tex <table_index>
```

This is much faster than re-running the full pipeline.

### Step 3: Diagnose and Fix

Common failure patterns — read `reference/latex-failure-patterns.md` for the full catalog:
- **Missing macro**: `\newcommand` defined in body (not preamble) → inject into preamble
- **`\resizebox` wrapping table**: pdflatex can't handle it → strip wrapper, retry
- **Font package not installed**: map to available fonts or install missing package
- **pgfplotstable with external data**: data file not found → inline the data
- **TikZ figure touches border**: textwidth detection wrong → increase page dimensions

After identifying the fix, modify the relevant parser in `src/paper_tool/` (`figure_extractor.py` or `table_extractor.py`).

### Step 4: Verify the Fix

```bash
uv run paper-tool add <paper_url> --force --rerender-tables
```

Check `papers/<paper_dir>/debug/table_NN.json` — the `renderer` field should now be `"latex"`. Then verify previously-working papers don't regress. See `reference/latex-failure-patterns.md` for the regression checklist.

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
| Runtime config | `config.yaml` (searches upward from cwd) |
| Secrets | `.env` (same search strategy) |
| Paper storage | `papers/` (configurable via `storage.papers_dir`) |
| Prompt templates | `prompts/analyzer.md`, `classifier.md`, `summarizer.md` |
| Debug artifacts | `papers/<paper_dir>/debug/` |

### PipelineContext Fields

When constructing LLM objects or calling step functions, reference these from `PipelineContext`:

| Field | Type | Usage |
|-------|------|-------|
| `llm_model` | `str` | Model name for text LLM calls |
| `llm_vision_model` | `str` | Model name for vision LLM calls |
| `llm_temperature` | `float` | Temperature for all LLM calls |
| `llm_max_input_tokens` | `int` | Truncation budget for paper text |
| `llm_max_output_tokens` | `int` | Max tokens for note generation |
| `llm_classifier_max_tokens` | `int` | Max tokens for classifier |
| `llm_translator_max_tokens` | `int` | Max tokens for caption translation |
| `llm_summarizer_max_tokens` | `int` | Max tokens for one-sentence summary |
| `analyzer_prompt` | `str \| None` | Custom analyzer system prompt |
| `classifier_prompt` | `str \| None` | Custom classifier system prompt |
| `summarizer_prompt` | `str \| None` | Custom summarizer system prompt |
| `max_figures` | `int` | Max figures per paper |
| `max_tables` | `int` | Max tables per paper |
| `rerender_figures` | `bool` | Force figure re-render |
| `rerender_tables` | `bool` | Force table re-render |
| `papers_dir` | `Path` | Root directory for paper storage |
| `notion_token` | `str` | Notion integration token |
| `notion_database_id` | `str` | Target Notion database ID |
| `notion_properties` | `dict` | Property name mapping |
| `notion_status_type` | `str` | `"checkbox"` or `"select"` |
| `openai_vision_api_key` | `str \| None` | Vision API key (falls back to text key) |
| `openai_vision_base_url` | `str \| None` | Vision API base URL |

See `reference/paths.md` for the complete config reference and `reference/notion-properties.md` for the Notion database schema.

## Project Structure

When investigating parser bugs, work in these source files:

```
src/paper_tool/
├── config.py             # Config, PipelineContext, get_config()
├── pipeline.py           # run_pipeline(), download_paper(), extract_paper_text()
├── figure_extractor.py   # Figure parsing + pdflatex rendering
├── table_extractor.py    # Table parsing + pdflatex/matplotlib rendering
├── pdf_parser.py         # PDF + LaTeX text extraction
├── downloaders/          # Arxiv, OpenReview metadata + PDF download
├── notion_service.py     # Notion API (pages, blocks, uploads)
├── llm_analyzer.py       # Full reading notes + translate_captions()
├── llm_classifier.py     # Paper classification
├── llm_summarizer.py     # One-sentence summary
├── llm_chat.py           # Multi-turn paper Q&A
└── llm_stream.py         # completion_to_text() — low-level LLM call
```

## Common Pitfalls

- **Config not found**: `config.yaml` searches upward from cwd. Run commands from within the project or a subdirectory.
- **Notion 404**: The database ID in `.env` may be wrong. Run `uv run paper-tool config check-db` to verify.
- **Thinking model token exhaustion**: Models like kimi-k2.5 consume many tokens in CoT. Increase `classifier_max_tokens` and `translator_max_tokens` in `config.yaml`.
- **`\include` not resolved**: `_expand_tex_includes` may miss non-standard paths. Check the merged `paper.tex`.
- **Figure rendered as blank PNG**: The image file in `figures/` may be PDF-format. The tool auto-converts PDF to PNG, but check `convert_pdf_figures()`.

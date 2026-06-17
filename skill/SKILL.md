---
name: paper-reading-workflow
description: |
  Process academic papers end-to-end: download from arxiv/OpenReview/URL, extract text and figures/tables from LaTeX, generate AI reading notes, classify, summarize, and publish to Notion or Lark (飞书). Use this skill whenever the user mentions 论文, paper, arxiv, reading notes, LaTeX渲染, 表格提取, 图表, Notion笔记, 飞书文档, paper-tool, or wants to process/import/analyze any academic paper. Also use when debugging figure or table rendering failures.
---

# Paper Reading Workflow

You are an agent that processes academic papers. Your job: take a paper URL (or name), download it, extract content, generate reading notes via LLM, and publish to Notion or Lark (飞书) with embedded figures and tables.

## Output Backend Selection

Check `config.yaml` → `output.backends` to determine where to publish. It is a list:
- `["notion"]` (default) → run `notion_write.py`
- `["lark"]` → run `lark_write.py`
- `["notion", "lark"]` → run both

Each backend has its own configuration directory under `backends/<name>/`:
- `backends/notion/backend.yaml` — token, database_id, property mappings
- `backends/lark/backend.yaml` — identity, parent folder token

If a backend config is missing or incomplete, the script will prompt for the missing values automatically. You can also copy the example files manually:

```bash
cp backends/notion/backend.yaml.example backends/notion/backend.yaml
cp backends/lark/backend.yaml.example backends/lark/backend.yaml
```

## Scripts

All scripts live in this skill's `skill/scripts/` directory. Run them with `uv run`:

```
uv run skill/scripts/download.py <url> [--papers-dir PATH]
uv run skill/scripts/extract_text.py <paper-dir> [--max-chars N]
uv run skill/scripts/extract_visuals.py <paper-dir> [--max-figures N] [--max-tables N] [--rerender] [--repair] [--no-repair] [--max-repair-attempts N] [--enable-llm-repair]
uv run skill/scripts/classify.py <paper-dir> [--model M] [--options-json JSON]
uv run skill/scripts/summarize.py <paper-dir> [--model M]
uv run skill/scripts/translate_captions.py <paper-dir> [--model M]
uv run skill/scripts/analyze.py <paper-dir> [--model M] [--format json|freeform]
uv run skill/scripts/notion_write.py <paper-dir> [--force] [--skip-images]
uv run skill/scripts/lark_write.py <paper-dir> [--force] [--skip-images]
uv run skill/scripts/notion_check.py <url>
uv run skill/scripts/debug_render.py <paper-dir> --type figure|table --index N
```

Every script outputs JSON to stdout:
```json
{"status": "ok", "message": "...", "outputs": {"key": "path"}}
{"status": "error", "error": "...", "message": "human-readable explanation"}
```

Exit code 0 = success, 1 = failure. Always check stdout JSON for details.

## Decision Flows

### User gives a paper URL

Full pipeline, run in order:

1. `notion_check.py <url>` — (Notion backend only) if exists and user didn't say "force/重新导入", tell them it's already there
2. `download.py <url>` — produces `papers/<id>/` with PDF, LaTeX source, metadata.json
3. `extract_text.py <paper-dir>` — produces text.txt
4. `extract_visuals.py <paper-dir>` — produces figures/*.png, tables/*.png, visuals.json
   - **Always run with `--repair`** so the agent automatically fixes LaTeX compile failures, matplotlib fallback, and clipped figures/tables without bothering the user.
   - The repair loop is bounded (`repair_max_attempts` in `config.yaml`) and writes per-attempt debug artifacts to `debug/*.attempt_*.quality.json`.
   - Only use `--enable-llm-repair` if rule-based repair repeatedly fails and you want to spend extra tokens; otherwise stay with the default rule-only repair.
5. **Check visuals.json** — look at `render_stats`. If any table still shows `matplotlib` fallback after repair, note it briefly but continue; do not ask the user what to do.
6. `classify.py <paper-dir>` — produces classification.json
7. `summarize.py <paper-dir>` — produces summary.txt
8. `translate_captions.py <paper-dir>` — produces captions.json (translated figure/table captions to Chinese)
9. `analyze.py <paper-dir>` — produces notes.md (full reading notes)
10. Publish to output backend(s) based on `config.yaml` → `output.backend`:
    - **notion**: `notion_write.py <paper-dir>` — creates Notion database page with everything
    - **lark**: `lark_write.py <paper-dir>` — creates Lark docx document with everything
    - **both**: run both scripts

After publish succeeds, report the page/document URL to the user.

### User gives a vague paper name (not a URL)

Search for the paper first:
- Try arxiv search: `https://arxiv.org/search/?query=<terms>`
- If found, confirm the paper with user, then run the full pipeline with the URL
- If not found, ask user for a direct URL or more specific title

### User wants batch import

They'll provide a list of URLs (maybe in a file, maybe in chat). For each URL:
1. Run the full pipeline above
2. Track progress in manifest — skip papers already marked "done"
3. If one fails, log the error and continue with the next
4. Report a summary at the end: N succeeded, M failed (with reasons)

### User says "re-analyze" or "换个模型重新分析"

Only re-run the LLM steps, not download/extract:
1. `analyze.py <paper-dir> --model <new-model>` — regenerates notes.md
2. `notion_write.py <paper-dir> --force` — archives old page, creates new one

### User reports figure/table rendering problems

This is the debugging flow. Read `skill/references/latex-failure-patterns.md` for the full catalog.

1. Check `<paper-dir>/visuals.json` — find which figures/tables used "matplotlib" fallback
2. Check `<paper-dir>/debug/` — read the .log and .tex files for the failing item
3. Diagnose the LaTeX error (common: undefined macro, missing package, resizebox wrapping)
4. Fix the issue in the relevant extractor script
5. `debug_render.py <paper-dir> --type table --index N` — re-render just that item
6. Verify the output PNG looks correct
7. If the fix is in extractor code, re-run on a few other papers to check for regression

### User wants to check/resume a paper

Read the manifest to see what's done. Run remaining steps from where it left off.

## Manifest

You maintain `papers/.manifest.json` yourself. Update it after each script call.

Schema:
```json
{
  "papers": {
    "<paper_id>": {
      "paper_dir": "papers/<id>_<title>/",
      "url": "https://...",
      "title": "Paper Title",
      "steps": {
        "download": {"status": "done", "at": "2026-06-10T14:30:00Z"},
        "extract_text": {"status": "done", "at": "..."},
        "extract_visuals": {"status": "done", "at": "...", "figures": 5, "tables": 3, "fallbacks": 1},
        "classify": {"status": "done", "at": "..."},
        "summarize": {"status": "done", "at": "..."},
        "translate_captions": {"status": "done", "at": "..."},
        "analyze": {"status": "done", "at": "..."},
        "notion_write": {"status": "done", "at": "...", "page_url": "https://notion.so/..."},
    "lark_write": {"status": "done", "at": "...", "doc_url": "https://xxx.feishu.cn/docx/..."}
      }
    }
  }
}
```

Status values: `"done"`, `"error"`, `"skipped"`, `"pending"`.
After each script call, update the relevant step based on stdout JSON.

## Paper Directory Structure

Each paper gets its own directory under `papers/`:
```
papers/{paper_id}_{safe_title}/
├── paper.pdf              ← download.py
├── paper.tex              ← download.py (arXiv only)
├── source/                ← download.py (raw LaTeX files)
├── figures/*.png          ← extract_visuals.py (rendered figures)
├── tables/*.png           ← extract_visuals.py (rendered tables)
├── debug/                 ← extract_visuals.py (LaTeX compile logs)
├── metadata.json          ← download.py
├── text.txt               ← extract_text.py
├── visuals.json           ← extract_visuals.py
├── classification.json    ← classify.py
├── summary.txt            ← summarize.py
├── captions.json          ← translate_captions.py
└── notes.md               ← analyze.py
```

## Configuration

Scripts read config from two files (searched upward from cwd):
- `.env` — secrets: OPENAI_API_KEY, NOTION_TOKEN, NOTION_DATABASE_ID
- `config.yaml` — model settings, token limits, Notion property mappings

If these don't exist, copy `config.yaml.example` to `config.yaml` and `.env.example` to `.env`, then fill in the required secrets.

## Error Recovery

| Script | Common failure | What to do |
|--------|---------------|------------|
| download.py | 404 / network timeout | Check URL is valid; retry once |
| extract_text.py | PDF corrupt or encrypted | Tell user; skip this paper |
| extract_visuals.py | No LaTeX source (non-arXiv) | Skip; set step to "skipped" |
| extract_visuals.py | pdflatex not installed | Tell user to install texlive |
| classify.py | LLM rate limit | Wait 30s and retry |
| analyze.py | LLM output too long / truncated | Retry with higher max_tokens |
| notion_write.py | Notion API 401 | Check NOTION_TOKEN in .env |
| notion_write.py | Image upload fails | Retry; if persistent, use --skip-images |
| lark_write.py | lark-cli not installed | Tell user to install lark-cli (`npm i -g @earendil-works/lark-cli`) |
| lark_write.py | auth needs refresh | lark-cli auto-refreshes; retry once if fails |
| lark_write.py | Image upload fails | Retry; if persistent, use --skip-images |

## References

- `skill/references/latex-failure-patterns.md` — Read when debugging render failures
- `skill/references/notion-properties.md` — Read when dealing with Notion schema issues
- `skill/assets/prompts/` — LLM prompt templates (analyzer.md, classifier.md, summarizer.md)

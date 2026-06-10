# Handoff: paper-read-workflow → Agent-oriented Skill

**Date:** 2026-06-10
**Project:** `/home/sparidae/projects/paper-read-workflow`
**Branch context:** `refactor/agent-oriented` (existing attempt, user says can be ignored)

---

## What the user wants

Transform the paper-reading-workflow project into a **Claude Code Skill** that lets an agent handle the full paper processing lifecycle. The user's core abstraction is three phases:

1. **Universal input** — arxiv URL, direct PDF, vague paper name, web tech report → all handled
2. **Local processing** — download LaTeX source, compile figures/tables, LLM generates analysis notes, everything saved locally with a manifest tracking what's been done
3. **Multi-backend output** — Notion today, Feishu tomorrow, dynamically embed images in notes

Their exact words: "论文/内容输入 → 内容处理 → 内容输出"

---

## What the user explicitly REJECTED

These came from my design proposals and the user pushed back hard:

- **No shell script wrappers** — No `analyze.sh`, `ingest.sh`, `extract.sh` wrapping Python code. The user called this "垃圾" (garbage). Direct CLI or direct Python imports only.
- **No over-engineered Python SDK** — No `PipelineContext` with 42 fields, no layered sub-configs (`LLMConfig`, `NotionConfig`, etc.), no `from_context()` factory pattern. The existing `refactor/agent-oriented` branch did this; the user said to ignore it.
- **No dedicated tooling for batch/chat/citation-refresh** — These are secondary tasks the agent can handle by composing basic tools. Not worth building dedicated commands for.

---

## What the user WANTS (affirmative)

- **Single-file thinking** — all skill logic lives in the skill folder, minimal indirection
- **CLI commands** — `uv run paper-tool <command>` as the agent's interface
- **Skill document as decision guide** — not API reference, but "if user says X, do Y, check Z"
- **Self-diagnosis built into the skill flow** — after figure/table rendering, the agent inspects results and can trigger debug/fix loops
- **`manifest.json` per paper** — track what steps are done, what model was used, render status, so agent can resume/skip

---

## Current codebase state (main branch)

Key files the next agent should read:

| File | What it does |
|------|-------------|
| `src/paper_tool/config.py` | Config singleton (`get_config()`) — hard dependency on filesystem |
| `src/paper_tool/pipeline.py` | ~450 line monolithic `run_pipeline()` |
| `src/paper_tool/llm_analyzer.py` | `LLMAnalyzer.__init__` calls `get_config()` — not injectable |
| `src/paper_tool/llm_classifier.py` | Same pattern |
| `src/paper_tool/llm_summarizer.py` | Same pattern |
| `src/paper_tool/notion_service.py` | Same pattern |
| `src/paper_tool/downloaders/` | Arxiv + OpenReview downloaders |
| `src/paper_tool/figure_extractor.py` | LaTeX figure extraction + pdflatex rendering |
| `src/paper_tool/table_extractor.py` | LaTeX table extraction + pdflatex/matplotlib rendering |
| `src/paper_tool/models.py` | `PaperMetadata`, `PaperNote`, `Classification`, `FigureInfo` |
| `src/paper_tool/cli.py` | Current CLI (typer): `add`, `batch`, `chat`, `config`, `serve` |

Existing skill: `.claude/skills/paper-reading-workflow/SKILL.md` — currently a CLI command reference manual, not an agent decision guide.

---

## Where the design conversation left off

I was over-thinking the architecture. The user wants radical simplicity. Their core insight:

> The agent doesn't need a Python SDK. It needs a few well-designed CLI commands and a skill doc telling it what to do.

The real design questions still open:

1. **What are the minimal CLI commands?** Likely something like `ingest`, `extract`, `analyze`, `export` — or maybe even fewer. The agent composes them.

2. **How does the skill doc guide the agent through self-diagnosis?** This is the key value-add. After `extract`, the agent should check render status. If matplotlib fallback happened, the skill should guide it to debug.

3. **How to handle config injection without over-engineering?** The current `get_config()` global singleton blocks agent control. But the solution might be as simple as CLI flags (`--model`, `--temperature`) rather than a `PipelineContext` dataclass.

4. **What does the `manifest.json` schema look like?** Per-paper state tracking so agents can resume.

5. **How to support multiple output backends cleanly?** Notion today, Feishu later.

---

## Suggested approach for next session

1. Read `main` branch code to understand current state (not the refactor branch)
2. Read current skill at `.claude/skills/paper-reading-workflow/SKILL.md`
3. Design from the **agent's perspective**: "I'm an agent, user says X, what commands do I run, what do I check?"
4. Keep it brutally simple — fewer files, fewer abstractions
5. Propose the CLI surface + skill doc structure, get user buy-in before coding

---

## Suggested skills

- `paper-reading-workflow` — to understand the current skill
- `skill-creator` — to design/iterate the new skill properly
- `brainstorming` — if the next agent needs to explore design directions before committing

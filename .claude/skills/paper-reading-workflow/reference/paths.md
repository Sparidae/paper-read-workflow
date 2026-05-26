# Paths Reference

## Project Layout

```
<project_root>/                    # Where config.yaml lives
├── config.yaml                    # Runtime configuration
├── config.yaml.example            # Reference config template
├── .env                           # Secrets (NOTION_TOKEN, API keys)
├── .env.example                   # Template for .env
├── notion_schema.yaml             # Notion database property definitions
├── pyproject.toml                 # Project metadata + dependencies
│
├── prompts/                       # LLM system prompts (configurable)
│   ├── analyzer.md                # Deep reading note generation
│   ├── classifier.md              # Paper classification
│   └── summarizer.md              # One-sentence summary
│
├── papers/                        # Downloaded papers (configurable via storage.papers_dir)
│   └── <arxiv_id>_<title>/        # Per-paper directory
│       ├── paper.pdf              # Downloaded PDF
│       ├── paper.tex              # Merged LaTeX source (arxiv only)
│       ├── figures/               # Extracted images + rendered PNGs
│       ├── tables/                # Rendered table PNGs
│       ├── source/                # Original LaTeX source tree (arxiv only)
│       └── debug/                 # Render logs + JSON status for each figure/table
│
├── scripts/                       # Standalone utility scripts
│   ├── add_paper.sh               # SSH convenience for remote server
│   ├── create_notion_db.py        # Create Notion database from schema
│   ├── migrate_papers.py          # Migrate old flat layout to per-paper dirs
│   └── rank_by_likes.py           # Rank papers by likes + citations
│
├── src/paper_tool/                # Main Python package
│   ├── cli.py                     # CLI entry point (Typer)
│   ├── config.py                  # Configuration loader
│   ├── pipeline.py                # Core processing orchestration
│   ├── pdf_parser.py              # PDF + LaTeX text extraction
│   ├── figure_extractor.py        # Figure extraction + LaTeX rendering
│   ├── table_extractor.py         # Table extraction + LaTeX/matplotlib rendering
│   ├── llm_analyzer.py            # LLM note generation + caption translation
│   ├── llm_classifier.py          # LLM paper classification
│   ├── llm_summarizer.py          # LLM one-sentence summary
│   ├── llm_chat.py                # Interactive paper Q&A
│   ├── llm_stream.py              # OpenAI client, streaming, StreamWindow
│   ├── notion_service.py          # Notion API (pages, blocks, uploads)
│   ├── notion_setup.py            # Database schema validation
│   ├── models.py                  # Data models
│   ├── retry.py                   # Retry decorator + functional utility
│   ├── logging_setup.py           # Rotating file log config
│   ├── citation_refresh.py        # Citation count refresh
│   ├── citations.py               # Semantic Scholar batch client
│   ├── server.py                  # FastAPI web server
│   └── downloaders/
│       ├── __init__.py            # Downloader dispatcher
│       ├── base.py                # Abstract base class
│       ├── arxiv.py               # Arxiv downloader
│       └── openreview.py          # OpenReview downloader
│
├── logs/
│   └── paper_tool.log             # Rotating log (10 MB × 5 backups)
│
└── .claude/skills/paper-reading-workflow/  # This skill
    ├── SKILL.md
    ├── scripts/
    └── reference/
```

## Config File Reference

### config.yaml — all keys

```yaml
llm:
  max_input_tokens: 100000         # Truncate paper text above this
  max_output_tokens: 100000        # Max tokens for note generation
  classifier_max_tokens: 8000      # Classification token budget
  temperature: 1                   # 0.0 = deterministic
  note_format: "freeform"          # "json" | "freeform"
  summarizer_max_tokens: 8000      # Summary token budget
  translator_max_tokens: 16000     # Caption translation token budget
  max_figures: 15                  # Max figures per paper
  rerender_figures: true           # Force re-render (skip cache)
  max_tables: 10                   # Max tables per paper
  rerender_tables: true            # Force re-render (skip cache)
  analyzer_prompt: "prompts/analyzer.md"
  classifier_prompt: "prompts/classifier.md"
  summarizer_prompt: "prompts/summarizer.md"

storage:
  papers_dir: "papers"             # Relative to project root or absolute

notion:
  properties:                      # Logical → Notion column name mapping
    title: "论文笔记"
    authors: "作者"
    abstract: "一句话摘要"
    source: "来源"
    url: "论文链接"
    published_date: "发表日期"
    added_date: "添加日期"
    tags: "研究领域"
    paper_type: "论文类型"
    institution: "来源机构"
    status: "阅读状态"
  status_type: "checkbox"          # "select" | "checkbox"
```

### .env — all keys

| Variable | Required | Description |
|----------|----------|-------------|
| `NOTION_TOKEN` | Yes | Notion Integration Token (`secret_xxx`) |
| `NOTION_DATABASE_ID` | Yes | 32-char hex database ID |
| `NOTION_PARENT_PAGE_ID` | No | Parent page for auto-creating database |
| `OPENAI_MODEL` | Yes | Model name (e.g., `gpt-4o`) |
| `OPENAI_API_KEY` | Yes | API key |
| `OPENAI_BASE_URL` | No | Custom endpoint (DeepSeek, Kimi, etc.) |
| `OPENAI_VISION_MODEL` | No | Multimodal model (defaults to text model) |
| `OPENAI_VISION_API_KEY` | No | Vision API key |
| `OPENAI_VISION_BASE_URL` | No | Vision endpoint |
| `OPENREVIEW_USERNAME` | No | OpenReview login |
| `OPENREVIEW_PASSWORD` | No | OpenReview password |

## URL Format Support

The tool accepts these URL patterns:

| Source | URL Pattern |
|--------|------------|
| Arxiv | `arxiv.org/abs/<id>` |
| Arxiv PDF | `arxiv.org/pdf/<id>` |
| alphaXiv | `alphaxiv.org/abs/<id>` |
| ar5iv | `ar5iv.labs.google.com/abs/<id>` |
| HuggingFace | `huggingface.co/papers/<id>` |
| OpenReview | `openreview.net/forum?id=<id>` |
| Bare ID | `2301.12345` (auto-detected as arxiv) |

## Config Search Order

1. `config.yaml` and `.env` are searched **upward from cwd** (up to 5 levels)
2. If not found, falls back to **project root** (2 levels above `src/paper_tool/config.py`)
3. `config.yaml` example acts as final fallback if no config.yaml exists

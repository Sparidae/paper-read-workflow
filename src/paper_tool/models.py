"""Data models for paper metadata and analysis results."""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional


class PaperSource(str, Enum):
    ARXIV = "Arxiv"
    OPENREVIEW = "OpenReview"
    UNKNOWN = "Unknown"


@dataclass
class PaperMetadata:
    """Structured metadata extracted from a paper source."""

    title: str
    authors: list[str]
    abstract: str
    source: PaperSource
    url: str
    paper_id: str
    published_date: Optional[date] = None
    tags: list[str] = field(default_factory=list)
    pdf_path: Optional[str] = None

    @property
    def authors_str(self) -> str:
        return ", ".join(self.authors)


@dataclass
class PaperNote:
    """
    LLM-generated reading note.

    Two modes:
    - Structured (note_format="json"): all section fields populated, raw_content=None
    - Freeform  (note_format="freeform"): raw_content holds the model's text output,
      all section fields are empty strings / empty lists
    """

    # Structured mode fields
    overview: str = ""
    research_problem: str = ""
    methodology: str = ""
    contributions: list[str] = field(default_factory=list)
    experiments: str = ""
    limitations: str = ""
    key_takeaways: list[str] = field(default_factory=list)

    # Freeform mode: raw model output (Markdown or plain text)
    raw_content: Optional[str] = None

    @property
    def is_freeform(self) -> bool:
        return self.raw_content is not None


@dataclass
class Classification:
    """LLM-generated classification tags for a paper (decoupled from note)."""

    paper_type: list[str] = field(default_factory=list)      # 论文类型
    research_areas: list[str] = field(default_factory=list)  # 研究领域
    institutions: list[str] = field(default_factory=list)    # 来源机构


@dataclass
class FigureInfo:
    """A single figure or table extracted from a paper's LaTeX source."""

    image_path: "Path"   # absolute path to the PNG/JPG file on disk
    caption: str         # cleaned caption text (LaTeX commands stripped)
    label: str = ""      # LaTeX \label value (e.g. "fig:overview")
    number: int = 0      # figure/table number in document order (1-based)
    kind: str = "figure" # "figure" or "table"
    render_backend: str = ""  # tables: "latex" / "matplotlib" / "cached"

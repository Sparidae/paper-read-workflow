"""Text extraction from PDF (PyMuPDF) and LaTeX source (pylatexenc)."""

from __future__ import annotations

from pathlib import Path


def extract_text(pdf_path: Path, max_chars: int | None = None) -> str:
    """
    Extract text from a PDF file.

    For very long papers, we prioritize:
      1. Abstract + Introduction (first 20% of pages)
      2. Method sections (middle)
      3. Conclusion (last ~10% of pages)

    This ensures the most informative sections are always included
    when truncation is needed.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)

    if total_pages == 0:
        return ""

    if max_chars is None:
        # Extract all text
        pages_text = [page.get_text() for page in doc]
        return "\n\n".join(pages_text)

    # Smart truncation: prioritize important sections
    # Allocate budget: 40% intro, 40% middle, 20% conclusion
    intro_end = max(1, int(total_pages * 0.25))
    conclusion_start = max(intro_end + 1, int(total_pages * 0.85))

    intro_pages = list(range(0, intro_end))
    middle_pages = list(range(intro_end, conclusion_start))
    conclusion_pages = list(range(conclusion_start, total_pages))

    intro_budget = int(max_chars * 0.40)
    conclusion_budget = int(max_chars * 0.20)
    middle_budget = max_chars - intro_budget - conclusion_budget

    def collect_pages(page_indices: list[int], budget: int) -> str:
        parts = []
        used = 0
        for i in page_indices:
            text = doc[i].get_text()
            if used + len(text) > budget:
                remaining = budget - used
                if remaining > 0:
                    parts.append(text[:remaining])
                break
            parts.append(text)
            used += len(text)
        return "\n\n".join(parts)

    intro_text = collect_pages(intro_pages, intro_budget)
    middle_text = collect_pages(middle_pages, middle_budget)
    conclusion_text = collect_pages(conclusion_pages, conclusion_budget)

    sections = [s for s in [intro_text, middle_text, conclusion_text] if s.strip()]
    return "\n\n[...]\n\n".join(sections)


def extract_text_from_latex(tex_path: Path, max_chars: int | None = None) -> str:
    """
    Convert LaTeX source to plain text using pylatexenc.

    Strips LaTeX commands and environments, preserving prose and math
    in a form that LLMs can easily understand.
    """
    from pylatexenc.latex2text import LatexNodes2Text

    raw = tex_path.read_text(errors="replace")

    # Remove common non-content blocks to reduce noise before conversion
    _STRIP_PATTERNS = [
        r"\\begin\{figure\}.*?\\end\{figure\}",
        r"\\begin\{table\}.*?\\end\{table\}",
        r"\\begin\{lstlisting\}.*?\\end\{lstlisting\}",
        r"\\begin\{verbatim\}.*?\\end\{verbatim\}",
        r"\\bibliographystyle\{[^}]*\}",
        r"\\bibliography\{[^}]*\}",
        r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    ]
    import re as _re
    for pat in _STRIP_PATTERNS:
        raw = _re.sub(pat, "", raw, flags=_re.DOTALL)

    try:
        converter = LatexNodes2Text()
        text = converter.latex_to_text(raw)
    except Exception:
        # Fallback: strip LaTeX commands with regex if pylatexenc fails
        text = _re.sub(r"\\[a-zA-Z]+\{[^}]*\}", " ", raw)
        text = _re.sub(r"\\[a-zA-Z]+", " ", text)
        text = _re.sub(r"[{}]", "", text)

    # Collapse excessive whitespace
    text = _re.sub(r"\n{3,}", "\n\n", text)
    text = _re.sub(r" {2,}", " ", text)
    text = text.strip()

    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... 内容已截断 ...]"

    return text

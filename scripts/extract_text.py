# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pymupdf>=1.24.0",
#     "pylatexenc>=2.10",
#     "pyyaml>=6.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""Extract text from a paper (PDF or LaTeX source)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import output_error, output_ok


def extract_from_pdf(pdf_path: Path, max_chars: int | None = None) -> str:
    """Extract text from PDF with smart truncation prioritizing key sections."""
    import fitz

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    if total_pages == 0:
        return ""

    if max_chars is None:
        return "\n\n".join(page.get_text() for page in doc)

    intro_end = max(1, int(total_pages * 0.25))
    conclusion_start = max(intro_end + 1, int(total_pages * 0.85))

    intro_pages = list(range(0, intro_end))
    middle_pages = list(range(intro_end, conclusion_start))
    conclusion_pages = list(range(conclusion_start, total_pages))

    intro_budget = int(max_chars * 0.40)
    conclusion_budget = int(max_chars * 0.20)
    middle_budget = max_chars - intro_budget - conclusion_budget

    def collect(page_indices: list[int], budget: int) -> str:
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

    sections = [
        collect(intro_pages, intro_budget),
        collect(middle_pages, middle_budget),
        collect(conclusion_pages, conclusion_budget),
    ]
    return "\n\n[...]\n\n".join(s for s in sections if s.strip())


def extract_from_latex(tex_path: Path, max_chars: int | None = None) -> str:
    """Convert LaTeX to plain text, stripping figures/tables/bibliography."""
    from pylatexenc.latex2text import LatexNodes2Text

    raw = tex_path.read_text(errors="replace")

    strip_patterns = [
        r"\\begin\{figure\}.*?\\end\{figure\}",
        r"\\begin\{table\}.*?\\end\{table\}",
        r"\\begin\{lstlisting\}.*?\\end\{lstlisting\}",
        r"\\begin\{verbatim\}.*?\\end\{verbatim\}",
        r"\\bibliographystyle\{[^}]*\}",
        r"\\bibliography\{[^}]*\}",
        r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    ]
    for pat in strip_patterns:
        raw = re.sub(pat, "", raw, flags=re.DOTALL)

    try:
        text = LatexNodes2Text().latex_to_text(raw)
    except Exception:
        text = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", " ", raw)
        text = re.sub(r"\\[a-zA-Z]+", " ", text)
        text = re.sub(r"[{}]", "", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()

    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated ...]"

    return text


def main():
    parser = argparse.ArgumentParser(description="Extract text from paper")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument(
        "--max-chars", type=int, default=400000, help="Max characters to extract"
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    if not paper_dir.is_dir():
        output_error(f"Paper directory not found: {paper_dir}")
        return

    tex_path = paper_dir / "paper.tex"
    pdf_path = paper_dir / "paper.pdf"
    output_path = paper_dir / "text.txt"

    source_type = None
    text = ""

    if tex_path.exists():
        text = extract_from_latex(tex_path, max_chars=args.max_chars)
        source_type = "latex"
    elif pdf_path.exists():
        text = extract_from_pdf(pdf_path, max_chars=args.max_chars)
        source_type = "pdf"
    else:
        output_error("No paper.tex or paper.pdf found in paper directory")
        return

    if not text.strip():
        output_error(f"Extracted empty text from {source_type}")
        return

    output_path.write_text(text, encoding="utf-8")

    output_ok(
        f"Extracted {len(text)} chars from {source_type}",
        source=source_type,
        char_count=len(text),
        outputs={"text": str(output_path)},
    )


if __name__ == "__main__":
    main()

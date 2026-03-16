"""Extract and convert paper figures from arXiv LaTeX source."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from paper_tool.models import FigureInfo


# Stay comfortably below Notion's 20 MB single-part upload limit
_MAX_FILE_BYTES = 18 * 1024 * 1024

# Supported image extensions in preference order (pdf last, needs conversion)
_IMG_EXTS = [".png", ".jpg", ".jpeg", ".pdf"]

# Matches \begin{figure} ... \end{figure} (including figure*)
_FIGURE_ENV = re.compile(
    r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}",
    re.DOTALL,
)

# Matches \includegraphics[optional]{path}
_INCLUDEGRAPHICS = re.compile(
    r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}"
)

# Matches \label{...}
_LABEL = re.compile(r"\\label\{([^}]+)\}")


# Strips LaTeX line comments: unescaped % to end of line
_TEX_COMMENT = re.compile(r"(?<!\\)%.*")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_tex_comments(text: str) -> str:
    """Remove LaTeX line comments (``%`` to end-of-line, ignoring ``\\%``)."""
    return _TEX_COMMENT.sub("", text)


def _extract_brace_content(text: str, pos: int) -> str:
    """Return content inside balanced braces, starting right after the '{' at pos."""
    depth = 1
    i = pos
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[pos : i - 1]


def _find_caption(env_text: str) -> str:
    """Extract and clean \\caption{...} from a figure environment string."""
    idx = env_text.find(r"\caption")
    if idx == -1:
        return ""
    after = env_text[idx + len(r"\caption"):].lstrip()

    # Skip optional short caption: \caption[short]{full}
    if after.startswith("["):
        close = after.find("]")
        if close != -1:
            after = after[close + 1 :].lstrip()

    if not after.startswith("{"):
        return ""

    raw = _extract_brace_content(after, 1)

    # Strip LaTeX commands while preserving their text argument
    raw = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", raw)
    raw = re.sub(r"\\[a-zA-Z]+", " ", raw)
    raw = re.sub(r"[{}]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _resolve_image(stem: str, figures_dir: Path) -> Optional[Path]:
    """
    Given an \\includegraphics argument (possibly with path prefix and no extension),
    return the matching file path in figures_dir, or None if not found.

    Matching is done by filename stem only (directory prefix is stripped) so that
    the flattened extraction structure in figures_dir is still correctly matched.
    """
    base = Path(stem).stem  # strip directory and extension
    for ext in _IMG_EXTS:
        candidate = figures_dir / (base + ext)
        if candidate.exists():
            return candidate
    return None


def _pdf_to_png(pdf_path: Path) -> Optional[Path]:
    """
    Rasterise the first page of a PDF figure to a PNG using PyMuPDF.
    Returns the PNG path, or None on failure.
    """
    import logging

    log = logging.getLogger(__name__)
    png_path = pdf_path.with_suffix(".png")
    if png_path.exists():
        return png_path

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            log.warning("PDF figure has no pages: %s", pdf_path.name)
            return None
        page = doc[0]
        # 2× scale gives ~150 dpi for a typical half-column figure → good quality
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(str(png_path))
        doc.close()
        return png_path
    except Exception as exc:
        log.warning("Failed to convert PDF figure %s: %s", pdf_path.name, exc)
        return None


def convert_pdf_figures(figures_dir: Path) -> int:
    """
    Batch-convert all PDF images in a figures directory to PNG.

    Skips files that already have a corresponding .png.
    Returns the number of successfully converted files.
    """
    if not figures_dir.is_dir():
        return 0

    converted = 0
    for pdf_file in sorted(figures_dir.glob("*.pdf")):
        if pdf_file.with_suffix(".png").exists():
            continue
        result = _pdf_to_png(pdf_file)
        if result is not None:
            converted += 1
    return converted


# ── Public API ────────────────────────────────────────────────────────────────

def parse_figures(
    tex_path: Path,
    figures_dir: Path,
    max_figures: int = 8,
) -> list[FigureInfo]:
    """
    Parse a merged LaTeX file and return FigureInfo objects for figures that
    have matching image files in figures_dir.

    Steps:
    1. Scan all \\begin{figure}...\\end{figure} environments in order.
    2. For each, extract \\includegraphics path and \\caption{} text.
    3. Locate the corresponding file in figures_dir (flattened).
    4. Convert .pdf figures to .png via PyMuPDF.
    5. Skip files that are missing, unconvertible, or exceed 18 MB.
    6. Stop after max_figures valid figures.
    """
    if not tex_path.exists() or not figures_dir.exists():
        return []

    try:
        tex = tex_path.read_text(errors="replace")
    except Exception:
        return []

    results: list[FigureInfo] = []
    fig_number = 0

    for m in _FIGURE_ENV.finditer(tex):
        env_text = _strip_tex_comments(m.group(1))

        ig_match = _INCLUDEGRAPHICS.search(env_text)
        if not ig_match:
            continue

        image_path = _resolve_image(ig_match.group(1).strip(), figures_dir)
        if image_path is None:
            continue

        # Convert PDF figures to PNG
        if image_path.suffix.lower() == ".pdf":
            converted = _pdf_to_png(image_path)
            if converted is None:
                continue
            image_path = converted

        # Enforce size limit
        try:
            if image_path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue

        caption = _find_caption(env_text)
        label_m = _LABEL.search(env_text)
        label = label_m.group(1) if label_m else ""

        fig_number += 1
        results.append(
            FigureInfo(
                image_path=image_path,
                caption=caption,
                label=label,
                number=fig_number,
            )
        )

        if len(results) >= max_figures:
            break

    return results

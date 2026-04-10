r"""Extract and render paper figures from arXiv LaTeX source.

Robustness strategy:
  1. Prefer direct extraction for plain ``\includegraphics`` figures.
  2. Fall back to standalone LaTeX rendering for TikZ / PGFPlots / minted /
     other figure environments that don't map to a single external image file.
  3. Record debug status files under ``figures/debug`` so rendering failures are
     visible instead of silently looking like "no images found".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from paper_tool.models import FigureInfo

# Stay comfortably below Notion's 20 MB single-part upload limit
_MAX_FILE_BYTES = 18 * 1024 * 1024

# Supported image extensions in preference order (pdf last, needs conversion)
_IMG_EXTS = [".png", ".jpg", ".jpeg", ".pdf"]

# Matches \begin{figure} ... \end{figure} (including figure*)
# Group 1 = "*" or "", group 2 = body
_FIGURE_ENV = re.compile(
    r"\\begin\{figure(\*?)\}(?:\[[^\]]*\])?(.*?)\\end\{figure\*?\}",
    re.DOTALL,
)

# Matches \includegraphics[optional]{path}
_INCLUDEGRAPHICS = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")

# Matches \label{...}
_LABEL = re.compile(r"\\label\{([^}]+)\}")


# Strips LaTeX line comments: unescaped % to end of line
_TEX_COMMENT = re.compile(r"(?<!\\)%.*")

_LATEX_TEMPLATE = r"""\documentclass[border=4pt]{standalone}
\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{bm}
\usepackage{booktabs}
\usepackage{array}
\usepackage{multirow}
\usepackage{colortbl}
\usepackage{caption}
\usepackage{subcaption}
\usepackage{adjustbox}
\usepackage{tikz}
\usepackage{pgfplots}
\usepackage{pgfplotstable}
\usepackage{minted}
\usepackage{float}
\pgfplotsset{compat=newest}
\setlength{\textwidth}{@@TEXT_WIDTH@@}
\setlength{\columnwidth}{@@COLUMN_WIDTH@@}
\setlength{\linewidth}{\columnwidth}
\setlength{\textheight}{@@TEXT_HEIGHT@@}
\providecommand{\parencite}[1]{[#1]}
\providecommand{\faGithub}{}
\providecommand{\faEnvelopeO}{}
@@RENEW_STUBS@@
@@PREAMBLE_MACROS@@
\begin{document}
\captionsetup{type=figure}
@@FIGURE_BODY@@
\end{document}
"""


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
    after = env_text[idx + len(r"\caption") :].lstrip()

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


def _brace_delta(text: str) -> int:
    return text.count("{") - text.count("}")


def _extract_preamble_macros(tex: str) -> str:
    """Extract multi-line preamble macros/settings used by figures."""
    preamble = tex.split(r"\begin{document}", 1)[0]
    starters = (
        r"\newcommand",
        r"\renewcommand",
        r"\def",
        r"\DeclareMathOperator",
        r"\providecommand",
        r"\newcolumntype",
        r"\definecolor",
        r"\colorlet",
        r"\let",
        r"\usetikzlibrary",
        r"\usepgfplotslibrary",
        r"\tikzset",
        r"\pgfplotsset",
    )
    lines = preamble.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s.startswith(starters):
            i += 1
            continue
        block = s
        balance = _brace_delta(s)
        i += 1
        while balance > 0 and i < len(lines):
            nxt = lines[i].rstrip()
            block += "\n" + nxt
            balance += _brace_delta(nxt)
            i += 1
        blocks.append(block)
    return "\n".join(blocks)


def _extract_renewcommand_stubs(tex: str) -> str:
    r"""Create ``\providecommand`` stubs for commands redefined in the preamble."""
    preamble = tex.split(r"\begin{document}", 1)[0]
    # Match both \renewcommand{\cmd} and \renewcommand\cmd (no-brace form)
    matches = re.findall(
        r"\\renewcommand\*?\s*(?:\{(\\[A-Za-z@]+)\}|(\\[A-Za-z@]+))", preamble
    )
    commands = sorted({cmd for pair in matches for cmd in pair if cmd})
    return "\n".join(f"\\providecommand{{{cmd}}}{{}}" for cmd in commands)


def _consume_balanced(text: str, start: int, open_char: str, close_char: str) -> int:
    if start >= len(text) or text[start] != open_char:
        return start
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _remove_command_calls(text: str, command: str, arg_count: int = 1) -> str:
    r"""Remove commands like ``\caption[short]{long}`` or ``\label{...}``."""
    needle = f"\\{command}"
    parts: list[str] = []
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            parts.append(text[pos:])
            break
        parts.append(text[pos:idx])
        j = idx + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "[":
            j = _consume_balanced(text, j, "[", "]")
        while j < len(text) and text[j].isspace():
            j += 1
        for _ in range(arg_count):
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == "{":
                j = _consume_balanced(text, j, "{", "}")
        pos = j
    return "".join(parts)


def _prepare_figure_body(env_text: str) -> str:
    r"""Keep the original figure-local LaTeX body, dropping ``\caption`` / ``\label``."""
    body = env_text
    body = re.sub(
        r"\\adjustbox\{[^}]*\}\{\s*\\begin\{minipage\}\{[^}]*\}.*?\\captionof\{table\}.*?\\end\{minipage\}\s*\}",
        "",
        body,
        flags=re.DOTALL,
    )
    body = re.sub(
        r"\\begin\{minipage\}\{[^}]*\}.*?\\captionof\{table\}.*?\\end\{minipage\}",
        "",
        body,
        flags=re.DOTALL,
    )
    body = _remove_command_calls(body, "captionof", arg_count=2)
    body = _remove_command_calls(body, "captionsetup")
    body = _remove_command_calls(body, "caption")
    body = _remove_command_calls(body, "caption*")
    body = _remove_command_calls(body, "label")
    body = re.sub(r"^\s*\\hfill\s*", "", body, flags=re.MULTILINE)
    return body.strip()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def _write_render_json(debug_dir: Path, stem: str, data: dict) -> None:
    """Persist a complete render-result record as JSON."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _clear_debug_artifacts(debug_dir: Path, stem: str) -> None:
    for suffix in (
        # ".latex.tex" is kept as a permanent render log
        ".latex.stdout.txt",
        ".latex.stderr.txt",
        ".latex.log",
        ".fallback.txt",
    ):
        (debug_dir / f"{stem}{suffix}").unlink(missing_ok=True)


def _copy_tree_contents(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, out)


def _trim_whitespace(image_path: Path, padding: int = 10) -> None:
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        arr = np.array(img)
        mask = (arr < 250).any(axis=2)
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            return
        top = max(0, rows[0] - padding)
        bottom = min(img.height, rows[-1] + padding + 1)
        left = max(0, cols[0] - padding)
        right = min(img.width, cols[-1] + padding + 1)
        img.crop((left, top, right, bottom)).save(str(image_path))
    except Exception:
        pass


def _image_touches_border(image_path: Path, margin: int = 8) -> bool:
    """Heuristic: detect likely clipping via edge occupancy, not single-pixel touches."""
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        arr = np.array(img)
        mask = (arr < 245).any(axis=2)
        if not mask.any():
            return False

        top_ratio = float(mask[:margin, :].mean())
        bottom_ratio = float(mask[-margin:, :].mean())
        left_ratio = float(mask[:, :margin].mean())
        right_ratio = float(mask[:, -margin:].mean())
        return bool(
            top_ratio > 0.20
            or bottom_ratio > 0.20
            or left_ratio > 0.20
            or right_ratio > 0.20
        )
    except Exception:
        return False


def _source_dir_from_tex(tex_path: Path) -> Path:
    return tex_path.parent / "source"


def _probe_textwidth(
    tex: str, source_dir: Optional[Path] = None
) -> tuple[str, str] | None:
    r"""Compile a minimal probe document to measure the paper's actual
    ``\textwidth`` *and* ``\columnwidth``.

    Extracts only the ``\documentclass`` and geometry-related lines from the
    preamble so that custom packages (tikz, biblatex, …) cannot break the probe.
    Returns ``(textwidth, columnwidth)`` as TeX length strings, or ``None``.
    """
    if shutil.which("pdflatex") is None:
        return None
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    dc_match = re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}", preamble)
    if not dc_match:
        return None

    probe_lines = [dc_match.group(0)]
    for m in re.finditer(r"\\usepackage(?:\[[^\]]*\])?\{geometry\}", preamble):
        probe_lines.append(m.group(0))
    for m in re.finditer(r"\\geometry\{[^}]+\}", preamble):
        probe_lines.append(m.group(0))
    for m in re.finditer(
        r"\\usepackage(?:\[[^\]]*\])?\{(?:neurips|iclr|acl|icml|nips|arxiv|aaai|jmlr|acmart)[^}]*\}",
        preamble,
    ):
        probe_lines.append(m.group(0))

    probe_src = "\n".join(probe_lines) + (
        "\n\\begin{document}\n"
        "\\typeout{PROBED_TEXTWIDTH=\\the\\textwidth}\n"
        "\\typeout{PROBED_COLUMNWIDTH=\\the\\columnwidth}\n"
        "\\end{document}\n"
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            if source_dir and source_dir.exists():
                _copy_tree_contents(source_dir, tmpdir)
            tex_file = tmpdir / "probe.tex"
            tex_file.write_text(probe_src, encoding="utf-8")
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-draftmode", "probe.tex"],
                cwd=tmpdir,
                capture_output=True,
                timeout=15,
            )
            stdout = result.stdout.decode("utf-8", errors="replace")
            tw_m = re.search(r"PROBED_TEXTWIDTH=([\d.]+)pt", stdout)
            cw_m = re.search(r"PROBED_COLUMNWIDTH=([\d.]+)pt", stdout)
            if tw_m:
                tw_in = float(tw_m.group(1)) / 72.27
                if 2.5 <= tw_in <= 9.0:
                    tw_str = f"{tw_in:.2f}in"
                    cw_str = tw_str
                    if cw_m:
                        cw_in = float(cw_m.group(1)) / 72.27
                        if 2.0 <= cw_in <= tw_in:
                            cw_str = f"{cw_in:.2f}in"
                    return tw_str, cw_str
    except Exception:
        pass
    return None


def _estimate_textwidth(tex: str) -> str:
    r"""Heuristic estimation of ``\textwidth`` from common document classes and
    style packages.  Used as fallback when the compilation probe fails."""
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    if not preamble:
        return "6.50in"

    # 1) Explicit \geometry{..., textwidth=X, ...}
    geom = re.search(r"\\geometry\{([^}]+)\}", preamble)
    if geom:
        tw = re.search(r"textwidth\s*=\s*([\d.]+\s*(?:in|cm|mm|pt))", geom.group(1))
        if tw:
            return tw.group(1).replace(" ", "")

    # 2) Known style packages
    style_widths = {
        "neurips": "5.50in",
        "nips": "5.50in",
        "iclr": "5.50in",
        "icml": "6.75in",
        "acl": "6.30in",
        "emnlp": "6.30in",
        "naacl": "6.30in",
        "aaai": "7.00in",
        "jmlr": "6.00in",
    }
    for pkg, width in style_widths.items():
        if re.search(rf"\\usepackage(?:\[[^\]]*\])?\{{{pkg}", preamble):
            return width

    # 3) Document class heuristics
    dc = re.search(r"\\documentclass(?:\[([^\]]*)\])?\{([^}]+)\}", preamble)
    if dc:
        options = dc.group(1) or ""
        classname = dc.group(2).lower()
        if classname == "ieeetran":
            return "7.16in"
        if classname == "llncs":
            return "4.75in"
        if classname.startswith("acm") or classname == "sig-alternate":
            return "7.00in"
        if classname.startswith("revtex"):
            return "3.40in"
        if "twocolumn" in options:
            return "7.16in"

    return "6.50in"


def _detect_textwidth(tex: str, source_dir: Optional[Path] = None) -> tuple[str, str]:
    r"""Determine the paper's ``\textwidth`` and ``\columnwidth``.

    Returns ``(textwidth, columnwidth)`` as TeX length strings.
    For single-column papers both values are identical.
    """
    probed = _probe_textwidth(tex, source_dir)
    if probed is not None:
        return probed
    tw = _estimate_textwidth(tex)
    if _is_twocolumn(tex):
        cw = f"{float(tw.replace('in', '')) * 0.48:.2f}in"
        return tw, cw
    return tw, tw


def _is_twocolumn(tex: str) -> bool:
    """Detect whether the paper uses a two-column layout."""
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    dc = re.search(r"\\documentclass(?:\[([^\]]*)\])?\{([^}]+)\}", preamble)
    if not dc:
        return False
    options = dc.group(1) or ""
    classname = dc.group(2).lower()
    if "twocolumn" in options:
        return True
    if classname in ("ieeetran", "sig-alternate"):
        return True
    if classname.startswith("acm"):
        return True
    return False


def _looks_like_latex_drawn_figure(env_text: str) -> bool:
    markers = (
        r"\begin{tikzpicture}",
        r"\begin{pgfplots}",
        r"\begin{minted}",
        r"\pgfplotstable",
        r"\tikzset",
        r"\usetikzlibrary",
        r"\resizebox",
        r"\adjustbox",
        r"\begin{subfigure}",
    )
    return any(marker in env_text for marker in markers)


def _render_figure_latex(
    figure_body: str,
    output_path: Path,
    preamble_macros: str,
    *,
    renew_stubs: str,
    figures_dir: Path,
    source_dir: Path,
    debug_dir: Path,
    stem: str,
    text_width: str = "6.50in",
    column_width: str = "",
) -> bool:
    """Compile a standalone figure and rasterise it to PNG."""
    if shutil.which("pdflatex") is None:
        return False

    col_width = column_width or text_width

    try:
        height_candidates = ["10in", "16in", "24in"]
        last_tex_src = ""

        for text_height in height_candidates:
            tex_src = (
                _LATEX_TEMPLATE.replace("@@RENEW_STUBS@@", renew_stubs)
                .replace("@@PREAMBLE_MACROS@@", preamble_macros)
                .replace("@@FIGURE_BODY@@", figure_body)
                .replace("@@TEXT_WIDTH@@", text_width)
                .replace("@@COLUMN_WIDTH@@", col_width)
                .replace("@@TEXT_HEIGHT@@", text_height)
            )
            last_tex_src = tex_src
            # Always persist the LaTeX source before compiling so it is
            # available for both success and failure paths.
            _write_text(debug_dir / f"{stem}.latex.tex", tex_src)

            with tempfile.TemporaryDirectory() as tmpdir_str:
                tmpdir = Path(tmpdir_str)
                _copy_tree_contents(source_dir, tmpdir)
                if figures_dir.exists():
                    tmp_fig_dir = tmpdir / "figures"
                    tmp_fig_dir.mkdir(parents=True, exist_ok=True)
                    for asset in figures_dir.glob("*"):
                        if asset.is_file():
                            shutil.copy2(asset, tmp_fig_dir / asset.name)

                tex_file = tmpdir / "figure.tex"
                tex_file.write_text(tex_src, encoding="utf-8")

                result = subprocess.run(
                    [
                        "pdflatex",
                        "-shell-escape",
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "figure.tex",
                    ],
                    cwd=tmpdir,
                    capture_output=True,
                    timeout=60,
                )

                pdf_file = tmpdir / "figure.pdf"
                log_file = tmpdir / "figure.log"
                if result.returncode != 0 or not pdf_file.exists():
                    _write_text(
                        debug_dir / f"{stem}.latex.stdout.txt",
                        result.stdout.decode("utf-8", errors="replace"),
                    )
                    _write_text(
                        debug_dir / f"{stem}.latex.stderr.txt",
                        result.stderr.decode("utf-8", errors="replace"),
                    )
                    if log_file.exists():
                        shutil.copy2(log_file, debug_dir / f"{stem}.latex.log")
                    continue

                import fitz  # PyMuPDF

                doc = fitz.open(str(pdf_file))
                if len(doc) == 0:
                    doc.close()
                    continue

                page = doc[0]
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                pix.save(str(output_path))
                doc.close()
                _trim_whitespace(output_path)

                if (
                    _image_touches_border(output_path)
                    and text_height != height_candidates[-1]
                ):
                    output_path.unlink(missing_ok=True)
                    continue

                _clear_debug_artifacts(debug_dir, stem)
                return True

        return False
    except Exception:
        _write_text(debug_dir / f"{stem}.latex.tex", locals().get("last_tex_src", ""))
        return False


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
    max_figures: int = 15,
    force_rerender: bool = False,
) -> list[FigureInfo]:
    """
    Parse a merged LaTeX file and return FigureInfo objects for figures that
    have matching image files in figures_dir.

    Steps:
    1. Scan all \\begin{figure}...\\end{figure} environments in order.
    2. For each, extract ALL \\includegraphics paths and \\caption{} text.
       Figures with multiple subfigures produce one FigureInfo per image.
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

    tex_for_env_scan = _strip_tex_comments(tex)

    source_dir = _source_dir_from_tex(tex_path)
    debug_dir = tex_path.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    preamble_macros = _extract_preamble_macros(tex)
    renew_stubs = _extract_renewcommand_stubs(tex)
    text_width, column_width = _detect_textwidth(tex, source_dir)

    # Collect source files available for LaTeX compilation
    source_files: list[str] = []
    if source_dir.exists():
        source_files.extend(
            str(f.relative_to(tex_path.parent))
            for f in sorted(source_dir.rglob("*"))
            if f.is_file()
        )
    if figures_dir.exists():
        source_files.extend(
            str(f.relative_to(tex_path.parent))
            for f in sorted(figures_dir.glob("*"))
            if f.is_file()
        )

    results: list[FigureInfo] = []
    fig_number = 0

    is_twocolumn = _is_twocolumn(tex)

    for env_index, m in enumerate(_FIGURE_ENV.finditer(tex_for_env_scan), start=1):
        is_starred = m.group(1) == "*"
        env_text = m.group(2)
        if not env_text.strip():
            continue
        ig_matches = list(_INCLUDEGRAPHICS.finditer(env_text))

        caption = _find_caption(env_text)
        label_m = _LABEL.search(env_text)
        label = label_m.group(1) if label_m else ""
        resolved_any = False

        col_width = text_width if is_starred or not is_twocolumn else column_width

        if not ig_matches or _looks_like_latex_drawn_figure(env_text):
            fig_number += 1
            rendered_path = figures_dir / f"latex_figure_env_{env_index:02d}.png"
            # Debug stem uses sequential figure number for clean naming
            stem = f"figure_{fig_number:02d}"
            if force_rerender:
                rendered_path.unlink(missing_ok=True)
            if not rendered_path.exists():
                ok = _render_figure_latex(
                    _prepare_figure_body(env_text),
                    rendered_path,
                    preamble_macros,
                    renew_stubs=renew_stubs,
                    figures_dir=figures_dir,
                    source_dir=source_dir,
                    debug_dir=debug_dir,
                    stem=stem,
                    text_width=text_width,
                    column_width=col_width,
                )
                if not ok:
                    _write_render_json(
                        debug_dir,
                        stem,
                        {
                            "kind": "figure",
                            "number": fig_number,
                            "env_index": env_index,
                            "status": "failed",
                            "renderer": "latex_failed",
                            "caption": caption,
                            "label": label,
                            "image": str(rendered_path.relative_to(tex_path.parent)),
                            "latex_tex": f"{stem}.latex.tex",
                            "stderr": f"{stem}.latex.stderr.txt"
                            if (debug_dir / f"{stem}.latex.stderr.txt").exists()
                            else None,
                            "source_files": source_files,
                        },
                    )
                    fig_number -= 1
                    if not ig_matches:
                        continue
                else:
                    try:
                        if rendered_path.stat().st_size <= _MAX_FILE_BYTES:
                            latex_source = (
                                (debug_dir / f"{stem}.latex.tex").read_text(
                                    encoding="utf-8", errors="replace"
                                )
                                if (debug_dir / f"{stem}.latex.tex").exists()
                                else None
                            )
                            _write_render_json(
                                debug_dir,
                                stem,
                                {
                                    "kind": "figure",
                                    "number": fig_number,
                                    "env_index": env_index,
                                    "status": "success",
                                    "renderer": "latex",
                                    "caption": caption,
                                    "label": label,
                                    "image": str(
                                        rendered_path.relative_to(tex_path.parent)
                                    ),
                                    "text_width": text_width,
                                    "column_width": col_width,
                                    "source_files": source_files,
                                    "latex_source": latex_source,
                                },
                            )
                            results.append(
                                FigureInfo(
                                    image_path=rendered_path,
                                    caption=caption,
                                    label=label,
                                    number=fig_number,
                                    kind="figure",
                                    render_backend="latex",
                                )
                            )
                            if len(results) >= max_figures:
                                break
                            continue
                    except OSError:
                        fig_number -= 1
                        continue
            elif rendered_path.exists():
                results.append(
                    FigureInfo(
                        image_path=rendered_path,
                        caption=caption,
                        label=label,
                        number=fig_number,
                        kind="figure",
                        render_backend="cached",
                    )
                )
                if len(results) >= max_figures:
                    break
                continue

        for ig_match in ig_matches:
            image_path = _resolve_image(ig_match.group(1).strip(), figures_dir)
            if image_path is None:
                continue
            resolved_any = True

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

            fig_number += 1
            fig_stem = f"figure_{fig_number:02d}"
            _write_render_json(
                debug_dir,
                fig_stem,
                {
                    "kind": "figure",
                    "number": fig_number,
                    "env_index": env_index,
                    "status": "success",
                    "renderer": "file",
                    "caption": caption,
                    "label": label,
                    "image": str(image_path.relative_to(tex_path.parent)),
                    "latex_source": None,
                    "source_files": None,
                },
            )
            results.append(
                FigureInfo(
                    image_path=image_path,
                    caption=caption,
                    label=label,
                    number=fig_number,
                    kind="figure",
                    render_backend="file",
                )
            )

            if len(results) >= max_figures:
                break

        if not resolved_any and ig_matches:
            _write_render_json(
                debug_dir,
                f"missing_figure_env_{env_index:02d}",
                {
                    "kind": "figure",
                    "env_index": env_index,
                    "status": "failed",
                    "renderer": "missing_assets",
                    "caption": caption,
                    "label": label,
                    "note": "includegraphics_found_but_no_matching_local_file",
                },
            )
        if len(results) >= max_figures:
            break

    return results

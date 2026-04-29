r"""Extract paper tables from arXiv LaTeX source.

Rendering pipeline:
  1. Extract table metadata from the merged LaTeX source.
  2. Prefer standalone LaTeX rendering to preserve table semantics/style.
  3. Fall back to a simpler matplotlib redraw as the last resort.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from paper_tool.models import FigureInfo

_MAX_FILE_BYTES = 18 * 1024 * 1024

# Matches \begin{table} ... \end{table} (including table*)
_TABLE_ENV = re.compile(
    r"\\begin\{table\*?\}(?:\[[^\]]*\])?(.*?)\\end\{table\*?\}",
    re.DOTALL,
)

# Full tabular environment
_TABULAR_FULL = re.compile(
    r"(\\begin\{(?:tabular|tabulary|tabularx)\*?\}(?:\{[^}]*\})?\{[^}]*\}.*?\\end\{(?:tabular|tabulary|tabularx)\*?\})",
    re.DOTALL,
)

_LABEL = re.compile(r"\\label\{([^}]+)\}")
_TEX_COMMENT = re.compile(r"(?<!\\)%.*")

_LATEX_TEMPLATE = r"""\documentclass[border=6pt]{standalone}
\usepackage{booktabs}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{bm}
\usepackage{array}
\usepackage{multirow}
\usepackage{xcolor}
\usepackage{colortbl}
\usepackage{makecell}
\usepackage{rotating}
\usepackage{graphicx}
\usepackage{adjustbox}
\usepackage{tabularx}
\usepackage{tabulary}
\usepackage{caption}
\usepackage{pifont}
\usepackage{xspace}
\setlength{\textwidth}{@@TEXT_WIDTH@@}
\setlength{\columnwidth}{\textwidth}
\setlength{\linewidth}{\textwidth}
@@RENEW_STUBS@@
@@PREAMBLE_MACROS@@
% Fallback stubs: filled only if still undefined after preamble macros
\providecommand{\parencite}[1]{[#1]}
\providecommand{\citep}[1]{[#1]}
\providecommand{\citet}[1]{#1}
\providecommand{\faGithub}{}
\providecommand{\faEnvelopeO}{}
\begin{document}
\begin{minipage}{\textwidth}
@@TABLE_BODY@@
\end{minipage}
\end{document}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_renewcommand_stubs(tex: str) -> str:
    r"""Create ``\providecommand`` stubs for commands redefined in the preamble.

    Without stubs, ``\renewcommand{\somecmd}`` fails if ``\somecmd`` was
    defined by a style package not loaded in our standalone template.
    """
    preamble = tex.split(r"\begin{document}", 1)[0]
    # Match both \renewcommand{\cmd} and \renewcommand\cmd (no-brace form)
    matches = re.findall(
        r"\\renewcommand\*?\s*(?:\{(\\[A-Za-z@]+)\}|(\\[A-Za-z@]+))", preamble
    )
    commands = sorted({cmd for pair in matches for cmd in pair if cmd})
    return "\n".join(f"\\providecommand{{{cmd}}}{{}}" for cmd in commands)


def _probe_textwidth(tex: str) -> str | None:
    r"""Compile a minimal probe document to measure actual ``\textwidth``."""
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
        "\\end{document}\n"
    )
    try:
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
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
            if tw_m:
                inches = float(tw_m.group(1)) / 72.27
                if 2.5 <= inches <= 9.0:
                    return f"{inches:.2f}in"
    except Exception:
        pass
    return None


def _estimate_textwidth(tex: str) -> str:
    r"""Heuristic estimation of ``\textwidth``."""
    preamble = (
        tex.split(r"\begin{document}", 1)[0] if r"\begin{document}" in tex else ""
    )
    if not preamble:
        return "6.50in"

    geom = re.search(r"\\geometry\{([^}]+)\}", preamble)
    if geom:
        tw = re.search(r"textwidth\s*=\s*([\d.]+\s*(?:in|cm|mm|pt))", geom.group(1))
        if tw:
            return tw.group(1).replace(" ", "")

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


def _detect_textwidth(tex: str) -> str:
    r"""Determine the paper's ``\textwidth`` — probe first, heuristics as fallback."""
    probed = _probe_textwidth(tex)
    if probed is not None:
        return probed
    return _estimate_textwidth(tex)


def _strip_tex_comments(text: str) -> str:
    return _TEX_COMMENT.sub("", text)


def _extract_brace_content(text: str, pos: int) -> str:
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
    idx = env_text.find(r"\caption")
    if idx == -1:
        return ""
    after = env_text[idx + len(r"\caption") :].lstrip()
    if after.startswith("["):
        close = after.find("]")
        if close != -1:
            after = after[close + 1 :].lstrip()
    if not after.startswith("{"):
        return ""
    raw = _extract_brace_content(after, 1)
    raw = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", raw)
    raw = re.sub(r"\\[a-zA-Z]+", " ", raw)
    raw = re.sub(r"[{}]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _brace_delta(text: str) -> int:
    """Rough brace-balance heuristic for multi-line macro extraction."""
    return text.count("{") - text.count("}")


def _is_incomplete_macro_definition(block: str) -> bool:
    patterns = (
        r"\\newcommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
        r"\\renewcommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
        r"\\providecommand\*?\s*(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*$",
    )
    return any(re.fullmatch(pattern, block.strip()) for pattern in patterns)


def _macro_dedupe_key(block: str) -> str | None:
    stripped = block.strip()
    patterns = (
        r"\\(?:newcommand|renewcommand|providecommand)\*?\s*(?:\{(\\[A-Za-z@]+)\}|(\\[A-Za-z@]+))",
        r"\\def\s*(\\[A-Za-z@]+)",
        r"\\DeclareMathOperator\*?\s*\{(\\[A-Za-z@]+)\}",
        r"\\let\s*(\\[A-Za-z@]+)",
        r"\\newcolumntype\s*\{([^}]+)\}",
        r"\\definecolor\s*\{([^}]+)\}",
        r"\\colorlet\s*\{([^}]+)\}",
    )
    for pattern in patterns:
        m = re.match(pattern, stripped)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        if groups:
            return groups[0]
    return None


def _extract_preamble_macros(tex: str) -> str:
    """
    Extract \\newcommand / \\def / \\DeclareMathOperator lines from the merged
    .tex file preamble so custom macros used in tables resolve correctly.
    Returns a block of LaTeX ready to drop into the standalone preamble.
    """
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
    )
    lines = preamble.splitlines()
    macros: list[str | None] = []
    macro_positions: dict[str, int] = {}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s.startswith(starters):
            i += 1
            continue

        block = s
        brace_balance = _brace_delta(s)
        i += 1
        while brace_balance > 0 and i < len(lines):
            nxt = lines[i].rstrip()
            block += "\n" + nxt
            brace_balance += _brace_delta(nxt)
            i += 1
        if _is_incomplete_macro_definition(block):
            continue
        key = _macro_dedupe_key(block)
        if key is not None and key in macro_positions:
            macros[macro_positions[key]] = None
        if key is not None:
            macro_positions[key] = len(macros)
        macros.append(block)
    return "\n".join(block for block in macros if block)


def _consume_balanced(text: str, start: int, open_char: str, close_char: str) -> int:
    """Return the index right after a balanced [] or {} group."""
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


def _remove_command_calls(text: str, command: str) -> str:
    r"""Remove occurrences of commands like ``\caption[short]{long}``.

    Consumes one optional ``[...]`` argument followed by ALL consecutive
    ``{...}`` arguments (handles multi-arg commands like
    ``\resizebox{width}{height}{content}``).
    """
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

        # One optional [short] argument
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "[":
            j = _consume_balanced(text, j, "[", "]")

        # All consecutive {mandatory} arguments
        while True:
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == "{":
                j = _consume_balanced(text, j, "{", "}")
            else:
                break

        pos = j

    return "".join(parts)


def _strip_wrapper_commands(text: str, commands: tuple[str, ...]) -> str:
    body = text
    for command in commands:
        body = _remove_command_calls(body, command)
    return body


def _prepare_table_body(env_text: str) -> str:
    r"""
    Keep the original table-local styling commands and tabular env, but strip
    float-only metadata like ``\caption`` / ``\label``.
    """
    body = _strip_wrapper_commands(env_text, ("caption", "caption*", "label"))
    # Strip \vspace / \vspace*: float-positioning tricks that shrink the
    # standalone bounding box (negative → clips bottom; positive → useless
    # whitespace).  Row extra-spacing inside tabular uses \\[Xpt] syntax.
    body = re.sub(r"\\vspace\*?\{[^}]*\}", "", body)
    # Strip negative \hspace / \hspace*: can shift content outside the
    # measured bounding box, clipping the rendered image horizontally.
    body = re.sub(r"\\hspace\*?\{-[^}]*\}", "", body)
    # Collapse blank lines: inside tabular column specs they produce \par
    # errors (\@@array failure); inside cells they're equally invalid.
    body = re.sub(r"\n[ \t]*\n", "\n", body)
    return body.strip()


def _prepare_table_body_retry(env_text: str) -> str:
    r"""Build a safer standalone variant without changing table semantics."""
    body = _prepare_table_body(env_text)
    body = _strip_wrapper_commands(
        body,
        (
            "resizebox",
            "scalebox",
            "adjustbox",
        ),
    )
    body = re.sub(r"\\begin\{center\}", "", body)
    body = re.sub(r"\\end\{center\}", "", body)
    body = re.sub(r"\\centering\b", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def _write_render_json(debug_dir: Path, stem: str, data: dict) -> None:
    """Persist a complete render-result record as JSON."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{stem}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_render_json(debug_dir: Path, stem: str) -> dict:
    """Load a previously written render-result record, or return {}."""
    p = debug_dir / f"{stem}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_status_renderer(debug_dir: Path, stem: str) -> str:
    return _read_render_json(debug_dir, stem).get("renderer", "")


def _render_table_latex_source(
    tex_src: str,
    output_path: Path,
    *,
    debug_dir: Path | None = None,
    stem: str = "table",
    status_note: str = "",
) -> bool:
    # Always persist the LaTeX source before compiling so it survives both
    # success and failure paths.
    if debug_dir is not None:
        _write_text(debug_dir / f"{stem}.latex.tex", tex_src)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tex_file = tmp / "table.tex"
            tex_file.write_text(tex_src, encoding="utf-8")

            result = subprocess.run(
                [
                    "pdflatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "table.tex",
                ],
                cwd=tmpdir,
                capture_output=True,
                timeout=30,
            )

            pdf_file = tmp / "table.pdf"
            log_file = tmp / "table.log"
            if result.returncode != 0 or not pdf_file.exists():
                if debug_dir is not None:
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
                return False

            import fitz  # PyMuPDF

            doc = fitz.open(str(pdf_file))
            if len(doc) == 0:
                doc.close()
                return False

            page = doc[0]
            mat = fitz.Matrix(2.5, 2.5)  # ~225 dpi at standard resolution
            pix = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(str(output_path))
            doc.close()
            return True
    except Exception:
        if debug_dir is not None:
            _write_text(
                debug_dir / f"{stem}.latex.stderr.txt",
                "python_exception during compilation",
            )
        return False


def _render_table_latex(
    table_body: str,
    output_path: Path,
    preamble_macros: str = "",
    *,
    renew_stubs: str = "",
    text_width: str = "6.50in",
    debug_dir: Path | None = None,
    stem: str = "table",
    retry_table_body: str | None = None,
) -> bool:
    """
    Compile `table_body` as a standalone LaTeX document and rasterise to PNG.

    Returns True on success, False on any failure (missing pdflatex,
    compilation error, empty PDF, …).
    """
    if not shutil.which("pdflatex"):
        return False

    attempts = [(table_body, "compile_error")]
    if retry_table_body and retry_table_body.strip() and retry_table_body != table_body:
        attempts.append((retry_table_body, "compile_error_retry"))

    for current_body, status_note in attempts:
        tex_src = (
            _LATEX_TEMPLATE.replace("@@RENEW_STUBS@@", renew_stubs)
            .replace("@@PREAMBLE_MACROS@@", preamble_macros)
            .replace("@@TABLE_BODY@@", current_body)
            .replace("@@TEXT_WIDTH@@", text_width)
        )
        if _render_table_latex_source(
            tex_src,
            output_path,
            debug_dir=debug_dir,
            stem=stem,
            status_note=status_note,
        ):
            return True

    return False


# ── matplotlib fallback ───────────────────────────────────────────────────────


def _clean_cell(text: str) -> str:
    text = re.sub(r"\\multicolumn\{\d+\}\{[^}]*\}\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\multirow\{\d+\}\{[^}]*\}\{([^}]*)\}", r"\1", text)
    for cmd in (
        "textbf",
        "textit",
        "underline",
        "emph",
        "textrm",
        "texttt",
        "textsc",
        "text",
    ):
        text = re.sub(rf"\\{cmd}\{{([^}}]*)\}}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_tabular_rows(env_text: str) -> list[list[str]]:
    tab_m = re.search(
        r"\\begin\{(?:tabular|tabulary|tabularx)\*?\}(?:\{[^}]*\})?\{[^}]*\}(.*?)\\end\{(?:tabular|tabulary|tabularx)\*?\}",
        env_text,
        re.DOTALL,
    )
    if not tab_m:
        return []
    content = tab_m.group(1)
    content = re.sub(r"\\(?:hline|toprule|midrule|bottomrule)\b", "", content)
    content = re.sub(r"\\cline\{[^}]*\}", "", content)
    content = re.sub(r"\\(?:rowcolor|cellcolor)(?:\[[^\]]*\])?\{[^}]*\}", "", content)
    raw_rows = re.split(r"\\\\(?:\[[^\]]*\])?", content)
    result: list[list[str]] = []
    for raw_row in raw_rows:
        raw_row = raw_row.strip()
        if not raw_row:
            continue
        cells = [_clean_cell(c) for c in raw_row.split("&")]
        if any(c for c in cells):
            result.append(cells)
    return result


def _trim_whitespace(image_path: Path, padding: int = 12) -> None:
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


def _render_table_matplotlib(
    rows: list[list[str]],
    output_path: Path,
    debug_dir: Path | None = None,
    stem: str = "table",
) -> bool:
    """Fallback: render a parsed 2D table as a booktabs-style PNG with matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.lines as mlines
        import matplotlib.pyplot as plt

        if not rows:
            return False

        n_cols = max(len(row) for row in rows)
        norm = [row + [""] * (n_cols - len(row)) for row in rows]
        header = norm[0]
        data = norm[1:] if len(norm) > 1 else [[""] * n_cols]

        fig_w = max(5.0, n_cols * 1.8)
        fig_h = max(1.2, (len(data) + 1) * 0.48 + 0.3)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.axis("off")

        tbl = ax.table(cellText=data, colLabels=header, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        tbl.scale(1.0, 1.18)
        tbl.auto_set_column_width(list(range(n_cols)))

        cells_dict = tbl.get_celld()
        for (row, col), cell in cells_dict.items():
            cell.set_linewidth(0)
            cell.set_edgecolor("white")
            cell.set_facecolor("white")
            if row == 0:
                cell.set_text_props(weight="bold")

        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        all_bboxes = [cell.get_window_extent(renderer) for cell in cells_dict.values()]
        header_bboxes = [
            cell.get_window_extent(renderer)
            for (row, _), cell in cells_dict.items()
            if row == 0
        ]
        x0_px = min(b.x0 for b in all_bboxes)
        x1_px = max(b.x1 for b in all_bboxes)
        y_top_px = max(b.y1 for b in all_bboxes)
        y_bottom_px = min(b.y0 for b in all_bboxes)
        y_hdr_bottom_px = min(b.y0 for b in header_bboxes)
        inv = fig.transFigure.inverted()

        def _hline(y_px: float, lw: float) -> None:
            x0_f = inv.transform((x0_px, y_px))[0]
            x1_f = inv.transform((x1_px, y_px))[0]
            y_f = inv.transform((x0_px, y_px))[1]
            fig.add_artist(
                mlines.Line2D(
                    [x0_f, x1_f],
                    [y_f, y_f],
                    transform=fig.transFigure,
                    color="black",
                    linewidth=lw,
                    clip_on=False,
                )
            )

        _hline(y_top_px, 1.5)
        _hline(y_hdr_bottom_px, 0.8)
        _hline(y_bottom_px, 1.5)

        fig.patch.set_facecolor("white")
        plt.savefig(
            str(output_path),
            dpi=150,
            bbox_inches="tight",
            pad_inches=0.05,
            facecolor="white",
            edgecolor="none",
        )
        plt.close(fig)
        _trim_whitespace(output_path)
        return True
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────────────


def parse_tables(
    tex_path: Path,
    tables_dir: Path,
    max_tables: int = 10,
    force_rerender: bool = False,
) -> list[FigureInfo]:
    """
    Parse a merged LaTeX file and return FigureInfo objects (kind="table") for
    tables rendered as PNG images.

    Rendering strategy per table:
      1. Keep the original table-local LaTeX body and compile with pdflatex.
      2. If LaTeX fails, write debug files into ``tables/debug`` and fall back
         to matplotlib.
      3. If ``force_rerender`` is True, ignore cached PNGs.
    """
    if not tex_path.exists():
        return []

    try:
        tex = tex_path.read_text(errors="replace")
    except Exception:
        return []

    tables_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = tex_path.parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    preamble_macros = _extract_preamble_macros(tex)
    renew_stubs = _extract_renewcommand_stubs(tex)
    text_width = _detect_textwidth(tex)

    results: list[FigureInfo] = []
    tbl_number = 0

    for m in _TABLE_ENV.finditer(tex):
        env_text = _strip_tex_comments(m.group(1))

        # Need at least a tabular environment inside
        tab_full = _TABULAR_FULL.search(env_text)
        if not tab_full:
            continue

        caption = _find_caption(env_text)
        label_m = _LABEL.search(env_text)
        label = label_m.group(1) if label_m else ""
        table_body = _prepare_table_body(env_text)
        retry_table_body = _prepare_table_body_retry(env_text)
        tabular_raw = tab_full.group(1)

        tbl_number += 1
        stem = f"table_{tbl_number:02d}"
        output_path = tables_dir / f"{stem}.png"
        render_backend = ""

        if output_path.exists() and not force_rerender:
            render_backend = _read_status_renderer(debug_dir, stem) or "cached"
        else:
            output_path.unlink(missing_ok=True)

            ok = _render_table_latex(
                table_body or tabular_raw,
                output_path,
                preamble_macros,
                renew_stubs=renew_stubs,
                text_width=text_width,
                debug_dir=debug_dir,
                stem=stem,
                retry_table_body=retry_table_body or tabular_raw,
            )
            if ok:
                render_backend = "latex"

            if not ok:
                rows = _parse_tabular_rows(env_text)
                ok = _render_table_matplotlib(
                    rows,
                    output_path,
                    debug_dir=debug_dir,
                    stem=stem,
                )
                if ok:
                    render_backend = "matplotlib"

            if not ok:
                # Record failure in JSON log
                _write_render_json(
                    debug_dir,
                    stem,
                    {
                        "kind": "table",
                        "number": tbl_number,
                        "status": "failed",
                        "caption": caption,
                        "label": label,
                        "latex_tex": f"{stem}.latex.tex"
                        if (debug_dir / f"{stem}.latex.tex").exists()
                        else None,
                        "stderr": f"{stem}.latex.stderr.txt"
                        if (debug_dir / f"{stem}.latex.stderr.txt").exists()
                        else None,
                    },
                )
                tbl_number -= 1
                continue

        try:
            if output_path.stat().st_size > _MAX_FILE_BYTES:
                tbl_number -= 1
                continue
        except OSError:
            tbl_number -= 1
            continue

        # Read the LaTeX source that was compiled (if available)
        latex_tex_path = debug_dir / f"{stem}.latex.tex"
        latex_source = (
            latex_tex_path.read_text(encoding="utf-8", errors="replace")
            if latex_tex_path.exists()
            else None
        )

        _write_render_json(
            debug_dir,
            stem,
            {
                "kind": "table",
                "number": tbl_number,
                "status": "success",
                "renderer": render_backend,
                "caption": caption,
                "label": label,
                "image": str(output_path.relative_to(tex_path.parent)),
                "text_width": text_width,
                "latex_source": latex_source,
            },
        )

        results.append(
            FigureInfo(
                image_path=output_path,
                caption=caption,
                label=label,
                number=tbl_number,
                kind="table",
                render_backend=render_backend,
            )
        )

        if len(results) >= max_tables:
            break

    return results

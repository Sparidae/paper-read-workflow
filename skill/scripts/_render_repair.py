# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openai>=1.0.0",
#     "pyyaml>=6.0",
#     "python-dotenv>=1.0.0",
#     "Pillow>=10.0.0",
# ]
# ///
"""Render quality checks and auto-repair loop for LaTeX figures/tables.

This module is used by _figure_extractor.py and _table_extractor.py to:
  1. Detect bad renders (missing file, compile failure, clipping, blank output).
  2. Apply rule-based fixes for known LaTeX failure patterns.
  3. Fall back to an LLM if rule-based fixes fail.
  4. Loop until the render is acceptable or max attempts are exhausted.

Every repair attempt is recorded in debug/<stem>.json so the history is inspectable.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_config, llm_call, load_prompt

# ── Public dataclasses ───────────────────────────────────────────────────────


@dataclass
class RenderQuality:
    """Result of checking a rendered PNG."""

    ok: bool
    reason: str = ""
    touches_border: bool = False
    is_blank: bool = False
    is_tiny: bool = False
    is_huge: bool = False
    extreme_aspect: bool = False


@dataclass
class RepairAttempt:
    """One repair iteration."""

    attempt: int
    strategy: str  # "rule" or "llm"
    fix_description: str
    success: bool
    quality: RenderQuality | None = None


@dataclass
class RepairResult:
    """Outcome of the whole repair loop."""

    success: bool
    final_tex_src: str | None = None
    attempts: list[RepairAttempt] = field(default_factory=list)
    message: str = ""


# ── Image quality checks ─────────────────────────────────────────────────────


def _image_touches_border(
    image_path: Path, margin: int = 8, threshold: float = 0.20
) -> bool:
    """Heuristic: detect likely clipping via edge occupancy."""
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
            top_ratio > threshold
            or bottom_ratio > threshold
            or left_ratio > threshold
            or right_ratio > threshold
        )
    except Exception:
        return False


def _image_is_blank(image_path: Path, threshold: int = 250) -> bool:
    """True if nearly all pixels are white."""
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        arr = np.array(img)
        return bool((arr < threshold).any(axis=2).mean() < 0.005)
    except Exception:
        return False


def _image_dimensions(image_path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        return img.width, img.height
    except Exception:
        return None


def _is_extreme_aspect(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return True
    ratio = max(width, height) / min(width, height)
    return ratio > 20


def check_render_quality(
    output_path: Path,
    render_backend: str,
    debug_dir: Path,
    stem: str,
) -> RenderQuality:
    """Inspect a rendered PNG and report whether it looks reasonable."""
    if not output_path.exists():
        return RenderQuality(ok=False, reason="output file missing")

    try:
        if output_path.stat().st_size == 0:
            return RenderQuality(ok=False, reason="output file is empty")
    except OSError:
        return RenderQuality(ok=False, reason="cannot stat output file")

    dims = _image_dimensions(output_path)
    if dims is None:
        return RenderQuality(ok=False, reason="cannot open image")

    width, height = dims
    quality = RenderQuality(ok=True)

    if width < 50 or height < 50:
        quality.is_tiny = True
        quality.ok = False
        quality.reason = f"image too small ({width}x{height})"
        return quality

    if width > 8000 or height > 8000:
        quality.is_huge = True
        quality.ok = False
        quality.reason = f"image too large ({width}x{height})"
        return quality

    if _is_extreme_aspect(width, height):
        quality.extreme_aspect = True
        quality.ok = False
        quality.reason = f"extreme aspect ratio ({width}x{height})"
        return quality

    if _image_is_blank(output_path):
        quality.is_blank = True
        quality.ok = False
        quality.reason = "image appears blank/white"
        return quality

    if render_backend == "latex" and _image_touches_border(output_path):
        quality.touches_border = True
        quality.ok = False
        quality.reason = "content touches image border (likely clipped)"
        return quality

    if render_backend == "matplotlib":
        # matplotlib fallback means LaTeX compile failed
        quality.ok = False
        quality.reason = "rendered with matplotlib fallback"
        return quality

    quality.reason = "render looks reasonable"
    return quality


# ── LaTeX log parsing ────────────────────────────────────────────────────────


def _read_latex_log(debug_dir: Path, stem: str) -> str:
    """Read the pdflatex log for a render attempt."""
    log_path = debug_dir / f"{stem}.latex.log"
    if log_path.exists():
        return log_path.read_text(encoding="utf-8", errors="replace")
    stderr_path = debug_dir / f"{stem}.latex.stderr.txt"
    if stderr_path.exists():
        return stderr_path.read_text(encoding="utf-8", errors="replace")
    return ""


def _parse_missing_packages(log: str) -> list[str]:
    """Extract missing package names from a LaTeX log."""
    # ! LaTeX Error: File `foo.sty' not found.
    pattern = re.compile(r"File [`']([^`']+\.sty)[`'] not found", re.IGNORECASE)
    return [m.group(1).removesuffix(".sty") for m in pattern.finditer(log)]


def _parse_undefined_commands(log: str) -> list[str]:
    """Extract undefined control sequences from a LaTeX log."""
    # ! Undefined control sequence.
    # l.123 \somecmd
    cmds: list[str] = []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if "Undefined control sequence" in line:
            # Look at the next few lines for the offending command
            for j in range(i + 1, min(i + 5, len(lines))):
                m = re.search(r"l\.\d+\s+(\\[A-Za-z@]+)", lines[j])
                if m:
                    cmds.append(m.group(1))
                    break
    return cmds


def _parse_missing_files(log: str) -> list[str]:
    """Extract missing external files (other than .sty) from a LaTeX log."""
    pattern = re.compile(
        r"File [`']([^`']+\.(?:dat|csv|tex|pdf|png|jpg|jpeg))[`'] not found",
        re.IGNORECASE,
    )
    return [m.group(1) for m in pattern.finditer(log)]


def _parse_undefined_colors(log: str) -> list[str]:
    """Extract undefined xcolor color names from a LaTeX log."""
    # Package xcolor Error: Undefined color `Foo'.
    pattern = re.compile(r"Undefined color [`']([^`']+)[`']", re.IGNORECASE)
    return [m.group(1) for m in pattern.finditer(log)]


def _summarize_errors(log: str) -> str:
    """Return a concise summary of the first few LaTeX errors."""
    error_lines = []
    for line in log.splitlines():
        if line.strip().startswith("!"):
            error_lines.append(line.strip())
        if len(error_lines) >= 5:
            break
    return "\n".join(error_lines) if error_lines else "no explicit error lines found"


# ── Rule-based fixes ─────────────────────────────────────────────────────────


# Common packages that standalone templates may miss.
_KNOWN_PACKAGES = {
    "siunitx": r"\usepackage{siunitx}",
    "xspace": r"\usepackage{xspace}",
    "pifont": r"\usepackage{pifont}",
    "makecell": r"\usepackage{makecell}",
    "rotating": r"\usepackage{rotating}",
    "threeparttable": r"\usepackage{threeparttable}",
    "arydshln": r"\usepackage{arydshln}",
    "hhline": r"\usepackage{hhline}",
    "diagbox": r"\usepackage{diagbox}",
    "numprint": r"\usepackage{numprint}",
    "dsfont": r"\usepackage{dsfont}",
    "bbm": r"\usepackage{bbm}",
    "mathrsfs": r"\usepackage{mathrsfs}",
    "calrsfs": r"\usepackage{calrsfs}",
    "fontawesome5": r"\usepackage{fontawesome5}",
    "fontawesome": r"\usepackage{fontawesome}",
    "mhchem": r"\usepackage[version=4]{mhchem}",
    "chemformula": r"\usepackage{chemformula}",
    "algorithmicx": r"\usepackage{algpseudocode}",
    "algpseudocode": r"\usepackage{algpseudocode}",
    "algorithm": r"\usepackage{algorithm}",
    "listings": r"\usepackage{listings}",
    "minted": r"\usepackage{minted}",
}

# Commands that are safe to stub out in standalone renderings.
_SAFE_STUBS = {
    r"\citep": r"\providecommand{\citep}[1]{[#1]}",
    r"\citet": r"\providecommand{\citet}[1]{#1}",
    r"\parencite": r"\providecommand{\parencite}[1]{[#1]}",
    r"\cite": r"\providecommand{\cite}[1]{[#1]}",
    r"\cref": r"\providecommand{\cref}[1]{#1}",
    r"\Cref": r"\providecommand{\Cref}[1]{#1}",
    r"\ref": r"\providecommand{\ref}[1]{#1}",
    r"\eqref": r"\providecommand{\eqref}[1]{(#1)}",
    r"\autoref": r"\providecommand{\autoref}[1]{#1}",
    r"\url": r"\providecommand{\url}[1]{\texttt{#1}}",
    r"\href": r"\providecommand{\href}[2]{#2}",
    r"\footnote": r"\providecommand{\footnote}[1]{}",
    r"\marginpar": r"\providecommand{\marginpar}[1]{}",
    r"\todo": r"\providecommand{\todo}[1]{}",
    r"\alert": r"\providecommand{\alert}[1]{#1}",
    r"\textcolor": r"\providecommand{\textcolor}[2]{#2}",
    r"\colorbox": r"\providecommand{\colorbox}[2]{#2}",
    r"\faGithub": r"\providecommand{\faGithub}{}",
    r"\faEnvelopeO": r"\providecommand{\faEnvelopeO}{}",
    r"\faCheck": r"\providecommand{\faCheck}{$\u2713$}",
    r"\faTimes": r"\providecommand{\faTimes}{$✗$}",
}


def _insert_after_documentclass(tex_src: str, insertion: str) -> str:
    """Insert a line right after the documentclass declaration."""
    m = re.search(r"(\\documentclass(?:\[[^\]]*\])?\{[^}]+\})", tex_src)
    if m:
        pos = m.end()
        return tex_src[:pos] + "\n" + insertion + tex_src[pos:]
    return insertion + "\n" + tex_src


def _ensure_package(tex_src: str, pkg: str) -> tuple[str, bool]:
    """Add a package to the preamble if not already present."""
    if re.search(rf"\\usepackage(?:\[[^\]]*\])?\{{{re.escape(pkg)}\}}", tex_src):
        return tex_src, False
    return _insert_after_documentclass(tex_src, f"\\usepackage{{{pkg}}}"), True


def _ensure_stubs(tex_src: str, cmds: list[str]) -> tuple[str, list[str]]:
    """Add providecommand stubs for commands that are undefined."""
    added: list[str] = []
    for cmd in cmds:
        stub = _SAFE_STUBS.get(cmd)
        if stub is None:
            # generic empty stub
            stub = f"\\providecommand{{{cmd}}}{{}}"
        if cmd not in tex_src:
            tex_src = _insert_after_documentclass(tex_src, stub)
            added.append(cmd)
    return tex_src, added


def _strip_resizebox_wrappers(tex_src: str) -> tuple[str, bool]:
    r"""Remove \resizebox / \scalebox / \adjustbox wrappers around tabular/figure bodies."""
    original = tex_src
    tex_src = re.sub(
        r"\\resizebox\{[^}]*\}\{[^}]*\}\{\s*(\\begin\{(?:tabular|tabulary|tabularx|tikzpicture|axis)\})",
        r"\1",
        tex_src,
        flags=re.DOTALL,
    )
    tex_src = re.sub(
        r"\\scalebox\{[^}]*\}\{\s*(\\begin\{(?:tabular|tabulary|tabularx|tikzpicture|axis)\})",
        r"\1",
        tex_src,
        flags=re.DOTALL,
    )
    # Strip matching closing braces that were originally for resizebox/scalebox
    tex_src = re.sub(
        r"(\\end\{(?:tabular|tabulary|tabularx|tikzpicture|axis)\})\s*\}",
        r"\1",
        tex_src,
        flags=re.DOTALL,
    )
    return tex_src, tex_src != original


def _strip_centering(tex_src: str) -> tuple[str, bool]:
    r"""Remove center environment and \centering inside the body."""
    original = tex_src
    tex_src = re.sub(r"\\begin\{center\}", "", tex_src)
    tex_src = re.sub(r"\\end\{center\}", "", tex_src)
    tex_src = re.sub(r"\\centering\b", "", tex_src)
    return tex_src, tex_src != original


def _increase_page_height(tex_src: str) -> tuple[str, bool]:
    """Bump standalone page height to reduce clipping."""
    # Match \setlength{\textheight}{...}
    m = re.search(r"(\\setlength\{\\textheight\}\{)([\d.]+)(in|cm|mm|pt)\}", tex_src)
    if m:
        prefix, value, unit = m.group(1), float(m.group(2)), m.group(3)
        new_value = value * 1.5
        if unit == "in" and new_value > 30:
            return tex_src, False
        new_src = (
            tex_src[: m.start()]
            + f"{prefix}{new_value:.2f}{unit}}}"
            + tex_src[m.end() :]
        )
        return new_src, True
    # If no textheight set, insert one near textwidth
    m = re.search(r"(\\setlength\{\\textwidth\}\{[^}]+\})", tex_src)
    if m:
        pos = m.end()
        return tex_src[:pos] + "\n\\setlength{\\textheight}{16in}" + tex_src[pos:], True
    return tex_src, False


def _add_standalone_height(tex_src: str) -> tuple[str, bool]:
    """Add a generous standalone max size to allow large content."""
    if "\\standaloneconfig" in tex_src:
        return tex_src, False
    insertion = (
        r"\standaloneconfig{multi=false, crop=true, border=6pt, maxsize={20in}{30in}}"
    )
    return _insert_after_documentclass(tex_src, insertion), True


def _fix_missing_math_fonts(tex_src: str) -> tuple[str, bool]:
    """Add common math font packages if log indicates missing symbols."""
    added = False
    for pkg in ("amssymb", "bm", "amsfonts", "mathrsfs", "dsfont", "bbm"):
        tex_src, was_added = _ensure_package(tex_src, pkg)
        added = added or was_added
    return tex_src, added


def _ensure_xcolor_dvipsnames(tex_src: str) -> tuple[str, bool]:
    """Ensure xcolor is loaded with the dvipsnames option.

    Many papers use named colors like ForestGreen, RoyalBlue, etc. that are
    only available when xcolor is loaded with the dvipsnames option.
    """
    if re.search(r"\\usepackage\[.*dvipsnames.*\]\{xcolor\}", tex_src):
        return tex_src, False
    if "\\usepackage{xcolor}" in tex_src:
        return tex_src.replace(
            "\\usepackage{xcolor}", "\\usepackage[dvipsnames]{xcolor}"
        ), True
    if "\\usepackage[dvipsnames]{xcolor}" in tex_src:
        return tex_src, False
    return _insert_after_documentclass(
        tex_src, "\\usepackage[dvipsnames]{xcolor}"
    ), True


def apply_rule_fix(
    tex_src: str,
    log: str,
    quality: RenderQuality,
    previous_fixes: list[str],
    kind: str = "figure",
) -> tuple[str, str] | tuple[None, None]:
    """Try a single rule-based fix. Return (new_tex_src, fix_description) or (None, None)."""
    # 1. Missing packages
    missing = _parse_missing_packages(log)
    for pkg in missing:
        if pkg in _KNOWN_PACKAGES and f"package:{pkg}" not in previous_fixes:
            new_src, _ = _ensure_package(tex_src, pkg)
            return new_src, f"package:{pkg}"

    # 1b. Undefined xcolor colors: load xcolor with dvipsnames option
    undefined_colors = _parse_undefined_colors(log)
    if undefined_colors and "xcolor:dvipsnames" not in previous_fixes:
        new_src, ok = _ensure_xcolor_dvipsnames(tex_src)
        if ok:
            return new_src, "xcolor:dvipsnames"

    # 2. Undefined commands
    undefined = _parse_undefined_commands(log)
    new_cmds = [c for c in undefined if f"stub:{c}" not in previous_fixes]
    if new_cmds:
        new_src, added = _ensure_stubs(tex_src, new_cmds)
        if added:
            return new_src, f"stub:{added[0]}"

    # 3. Missing external files (data/images)
    missing_files = _parse_missing_files(log)
    for f in missing_files:
        key = f"missing_file:{f}"
        if key not in previous_fixes:
            # We cannot create missing files here, but we can stub out the command that uses them
            # A generic fix is to add an empty \providecommand for \pgfplotstableread etc.
            if (
                "pgfplotstableread" in log
                and "pgfplotstableread_inline" not in previous_fixes
            ):
                stub = r"\providecommand{\pgfplotstableread}[2][]{}"
                return _insert_after_documentclass(
                    tex_src, stub
                ), "pgfplotstableread_inline"
            return None, None

    # 4. Quality-driven fixes
    if not quality.ok:
        if quality.touches_border and "increase_page_height" not in previous_fixes:
            new_src, ok = _increase_page_height(tex_src)
            if ok:
                return new_src, "increase_page_height"
            new_src, ok = _add_standalone_height(tex_src)
            if ok:
                return new_src, "add_standalone_height"

        if quality.is_blank or quality.extreme_aspect or quality.is_tiny:
            if kind == "table" and "strip_resizebox" not in previous_fixes:
                new_src, ok = _strip_resizebox_wrappers(tex_src)
                if ok:
                    return new_src, "strip_resizebox"
            if "strip_centering" not in previous_fixes:
                new_src, ok = _strip_centering(tex_src)
                if ok:
                    return new_src, "strip_centering"

    # 5. Generic table fixes
    if kind == "table":
        if "strip_resizebox" not in previous_fixes:
            new_src, ok = _strip_resizebox_wrappers(tex_src)
            if ok:
                return new_src, "strip_resizebox"
        if "strip_centering" not in previous_fixes:
            new_src, ok = _strip_centering(tex_src)
            if ok:
                return new_src, "strip_centering"

    return None, None


# ── LLM-based repair ─────────────────────────────────────────────────────────


def apply_llm_fix(
    tex_src: str,
    log: str,
    quality: RenderQuality,
    kind: str = "figure",
    model: str | None = None,
) -> str | None:
    """Ask an LLM to fix the standalone LaTeX source. Returns fixed source or None."""
    config = load_config()
    llm_config = config.get("llm", {})

    if not llm_config.get("enable_llm_render_repair", False):
        return None

    try:
        prompt = load_prompt("render_repair.md")
    except SystemExit:
        prompt = """You are a LaTeX debugging assistant. A standalone figure/table failed to compile or rendered poorly.
Fix the LaTeX source so it compiles and produces a clean PNG. Output ONLY the corrected LaTeX source, no explanation."""

    user_text = f"""Kind: {kind}
Quality issue: {quality.reason}

=== pdflatex log / error summary ===
{_summarize_errors(log)}

=== failing standalone LaTeX source ===
{tex_src}
"""

    try:
        fixed = llm_call(
            system=prompt,
            user=user_text,
            model=model
            or llm_config.get("repair_model")
            or os.getenv("OPENAI_MODEL", "gpt-4o"),
            max_tokens=llm_config.get("repair_max_output_tokens", 16000),
            temperature=0.1,
        )
    except Exception:
        return None

    # Extract LaTeX if wrapped in markdown fences
    m = re.search(r"```(?:latex|tex)?\n(.*?)\n```", fixed, re.DOTALL)
    if m:
        fixed = m.group(1)

    fixed = fixed.strip()
    if not fixed.startswith("\\documentclass"):
        return None
    return fixed


# ── Repair loop ──────────────────────────────────────────────────────────────


def _write_attempt_artifacts(
    debug_dir: Path,
    stem: str,
    attempt: int,
    tex_src: str,
    log: str,
    quality: RenderQuality,
) -> None:
    """Persist per-attempt source/log for later inspection."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{stem}.attempt_{attempt}.latex.tex").write_text(
        tex_src, encoding="utf-8", errors="replace"
    )
    if log:
        (debug_dir / f"{stem}.attempt_{attempt}.latex.log").write_text(
            log, encoding="utf-8", errors="replace"
        )
    (debug_dir / f"{stem}.attempt_{attempt}.quality.json").write_text(
        json.dumps(
            {
                "ok": quality.ok,
                "reason": quality.reason,
                "touches_border": quality.touches_border,
                "is_blank": quality.is_blank,
                "is_tiny": quality.is_tiny,
                "is_huge": quality.is_huge,
                "extreme_aspect": quality.extreme_aspect,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def repair_render(
    tex_src: str,
    output_path: Path,
    debug_dir: Path,
    stem: str,
    render_func: Callable[[str, Path], bool],
    *,
    kind: str = "figure",
    max_attempts: int = 3,
    enable_llm: bool = False,
    llm_model: str | None = None,
) -> RepairResult:
    """Render, check quality, repair, and re-render in a loop.

    Args:
        tex_src: initial standalone LaTeX source.
        output_path: where the final PNG should be written.
        debug_dir: directory for debug artifacts.
        stem: base filename stem for debug files.
        render_func: callable(tex_src, output_path) -> bool that compiles and rasterises.
        kind: "figure" or "table".
        max_attempts: maximum repair iterations (including the initial render).
        enable_llm: whether to call an LLM after rule-based fixes are exhausted.
        llm_model: optional model override for LLM repair.

    Returns:
        RepairResult describing the final outcome.
    """
    current_tex = tex_src
    attempts: list[RepairAttempt] = []
    previous_fixes: list[str] = []

    for attempt in range(1, max_attempts + 1):
        output_path.unlink(missing_ok=True)
        ok = render_func(current_tex, output_path)
        backend = "latex" if ok else ""
        quality = check_render_quality(output_path, backend, debug_dir, stem)

        log = _read_latex_log(debug_dir, stem)
        _write_attempt_artifacts(debug_dir, stem, attempt, current_tex, log, quality)

        if quality.ok:
            return RepairResult(
                success=True,
                final_tex_src=current_tex,
                attempts=attempts,
                message=f"render accepted after {attempt} attempt(s)",
            )

        if attempt >= max_attempts:
            attempts.append(
                RepairAttempt(
                    attempt=attempt,
                    strategy="final_check",
                    fix_description="no_more_attempts",
                    success=False,
                    quality=quality,
                )
            )
            break

        # Try rule-based fix first
        strategy = "rule"
        fixed_tex, fix_description = apply_rule_fix(
            current_tex, log, quality, previous_fixes, kind=kind
        )

        # If rules exhausted and this is the last slot, optionally try LLM
        if fixed_tex is None and enable_llm and attempt == max_attempts - 1:
            strategy = "llm"
            fix_description = "llm_diagnose"
            fixed_tex = apply_llm_fix(
                current_tex, log, quality, kind=kind, model=llm_model
            )

        attempts.append(
            RepairAttempt(
                attempt=attempt,
                strategy=strategy,
                fix_description=fix_description or "no_fix_found",
                success=False,
                quality=quality,
            )
        )

        if fixed_tex is None:
            break

        previous_fixes.append(fix_description)
        current_tex = fixed_tex

    return RepairResult(
        success=False,
        final_tex_src=current_tex,
        attempts=attempts,
        message=f"render still bad after {len(attempts)} attempt(s): {quality.reason}",
    )

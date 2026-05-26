#!/usr/bin/env bash
# Extract and standalone-render a single figure from paper.tex.
# Usage: debug-figure.sh <paper.tex> <figure_index> [output_dir]
#
# Extracts the Nth figure environment (0-indexed) from the merged paper.tex,
# builds a standalone LaTeX document, compiles with pdflatex, and produces a PNG.
# Much faster than re-running the full paper-tool pipeline for debugging.

set -euo pipefail

PAPER_TEX="${1:?Usage: debug-figure.sh <paper.tex> <figure_index> [output_dir]}"
FIG_INDEX="${2:?Usage: debug-figure.sh <paper.tex> <figure_index> [output_dir]}"
OUT_DIR="${3:-/tmp/debug-figure}"

if [ ! -f "$PAPER_TEX" ]; then
    echo "ERROR: $PAPER_TEX not found"
    exit 1
fi

PAPER_DIR="$(dirname "$(realpath "$PAPER_TEX")")"
FIGURES_DIR="$PAPER_DIR/figures"
SOURCE_DIR="$PAPER_DIR/source"

mkdir -p "$OUT_DIR"

echo "=== Debug Figure [$FIG_INDEX] ==="
echo "Paper tex:  $PAPER_TEX"
echo "Figures dir: $FIGURES_DIR"
echo "Output dir: $OUT_DIR"
echo ""

# --- Extract preamble (before \begin{document}) ---
PREAMBLE=$(awk '/\\begin\{document\}/{exit} {print}' "$PAPER_TEX")

# --- Extract user-defined macros from preamble ---
PREAMBLE_MACROS=$(echo "$PREAMBLE" | grep -oP '(\\newcommand\*?\{[^}]*\}\{[^}]*\}|\\def\s*\\[a-zA-Z@]+\{[^}]*\}|\\definecolor\{[^}]*\}\{[^}]*\}\{[^}]*\})' || true)

# --- Extract renewcommand stubs from preamble ---
RENEW_STUBS=$(echo "$PREAMBLE" | grep -oP '\\renewcommand\*?\{[^}]+\}' | sed 's/\\renewcommand\*?/\\providecommand/' | sed 's/$/{}/' || true)

# --- Extract the Nth figure environment ---
# Use python for robust brace-balanced extraction
FIG_BODY=$(python3 -c "
import re, sys

text = open('$PAPER_TEX', 'r').read()

# Find all \begin{figure}...\end{figure} environments (brace-balanced)
pattern = re.compile(r'\\\\begin\{figure(\*?)\}(?:\[[^\]]*\])?', re.DOTALL)
matches = list(pattern.finditer(text))

if $FIG_INDEX >= len(matches):
    print(f'ERROR: Only {len(matches)} figures found (index $FIG_INDEX out of range)', file=sys.stderr)
    sys.exit(1)

m = matches[$FIG_INDEX]
star = m.group(1)
start = m.start()
# Find the matching \end{figure}
env_start = '\\\\begin{figure' + star + '}'
env_end = '\\\\end{figure' + star + '}'

depth = 1
pos = m.end()
while depth > 0 and pos < len(text):
    nxt_start = text.find(env_start, pos)
    nxt_end = text.find(env_end, pos)
    if nxt_end == -1:
        break
    if nxt_start != -1 and nxt_start < nxt_end:
        depth += 1
        pos = nxt_start + len(env_start)
    else:
        depth -= 1
        pos = nxt_end + len(env_end)

body = text[m.start():pos]

# Strip \caption{...} and \label{...}
body = re.sub(r'\\\\caption\*?(?:\[[^\]]*\])?\{[^}]*\}', '', body)
body = re.sub(r'\\\\label\{[^}]*\}', '', body)

print(body)
")

if [ $? -ne 0 ]; then
    echo "$FIG_BODY"
    exit 1
fi

# --- Build standalone LaTeX document ---
cat > "$OUT_DIR/figure.tex" << 'TEXEOF'
\documentclass[border=4pt]{standalone}
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
\setlength{\textwidth}{6.50in}
\setlength{\columnwidth}{6.50in}
\setlength{\linewidth}{\columnwidth}
\setlength{\textheight}{10in}
\providecommand{\parencite}[1]{[#1]}
\providecommand{\faGithub}{}
\providecommand{\faEnvelopeO}{}
TEXEOF

# Append renew stubs
echo "$RENEW_STUBS" >> "$OUT_DIR/figure.tex"

# Append preamble macros
echo "$PREAMBLE_MACROS" >> "$OUT_DIR/figure.tex"

# Append body
cat >> "$OUT_DIR/figure.tex" << 'TEXEOF'
\begin{document}
\captionsetup{type=figure}
TEXEOF

echo "$FIG_BODY" >> "$OUT_DIR/figure.tex"

echo '\end{document}' >> "$OUT_DIR/figure.tex"

echo "--- standalone LaTeX written to $OUT_DIR/figure.tex ---"

# --- Compile ---
# Copy figures and source to tempdir for pdflatex
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cp "$OUT_DIR/figure.tex" "$TMPDIR/figure.tex"
if [ -d "$SOURCE_DIR" ]; then
    cp -r "$SOURCE_DIR"/* "$TMPDIR/" 2>/dev/null || true
fi
if [ -d "$FIGURES_DIR" ]; then
    mkdir -p "$TMPDIR/figures"
    cp "$FIGURES_DIR"/* "$TMPDIR/figures/" 2>/dev/null || true
fi

echo "--- compiling with pdflatex ---"
cd "$TMPDIR"
if pdflatex -shell-escape -interaction=nonstopmode -halt-on-error figure.tex > "$OUT_DIR/figure.stdout" 2> "$OUT_DIR/figure.stderr"; then
    echo "pdflatex OK"
else
    echo "pdflatex FAILED (see $OUT_DIR/figure.stdout and $OUT_DIR/figure.stderr)"
    if [ -f figure.log ]; then
        cp figure.log "$OUT_DIR/figure.log"
        echo "--- log excerpt ---"
        grep -E '^!|Error|Warning' figure.log | head -20 || true
    fi
    exit 1
fi

# --- Convert PDF to PNG ---
if [ -f figure.pdf ]; then
    python3 -c "
import fitz
doc = fitz.open('figure.pdf')
if len(doc) > 0:
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    pix.save('$OUT_DIR/figure.png')
    print(f'PNG saved to $OUT_DIR/figure.png ({pix.width}x{pix.height})')
else:
    print('ERROR: PDF has no pages')
doc.close()
"
else
    echo "ERROR: figure.pdf not generated"
    exit 1
fi

echo ""
echo "=== Done ==="
echo "Output files in $OUT_DIR/:"
ls -la "$OUT_DIR/"
echo ""
echo "PNG: $OUT_DIR/figure.png"

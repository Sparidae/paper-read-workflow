#!/usr/bin/env bash
# Extract and standalone-render a single table from paper.tex.
# Usage: debug-table.sh <paper.tex> <table_index> [output_dir]
#
# Extracts the Nth table environment (0-indexed) from the merged paper.tex,
# builds a standalone LaTeX document, compiles with pdflatex, and produces a PNG.
# Strips resizebox/scalebox wrappers and center environments that interfere
# with standalone compilation.

set -euo pipefail

PAPER_TEX="${1:?Usage: debug-table.sh <paper.tex> <table_index> [output_dir]}"
TAB_INDEX="${2:?Usage: debug-table.sh <paper.tex> <table_index> [output_dir]}"
OUT_DIR="${3:-/tmp/debug-table}"

if [ ! -f "$PAPER_TEX" ]; then
    echo "ERROR: $PAPER_TEX not found"
    exit 1
fi

PAPER_DIR="$(dirname "$(realpath "$PAPER_TEX")")"
SOURCE_DIR="$PAPER_DIR/source"

mkdir -p "$OUT_DIR"

echo "=== Debug Table [$TAB_INDEX] ==="
echo "Paper tex:  $PAPER_TEX"
echo "Output dir: $OUT_DIR"
echo ""

# --- Extract preamble (before \begin{document}) ---
PREAMBLE=$(awk '/\\begin\{document\}/{exit} {print}' "$PAPER_TEX")

# --- Extract user-defined macros from preamble ---
PREAMBLE_MACROS=$(echo "$PREAMBLE" | grep -oP '(\\newcommand\*?\{[^}]*\}\{[^}]*\}|\\def\s*\\[a-zA-Z@]+\{[^}]*\}|\\definecolor\{[^}]*\}\{[^}]*\}\{[^}]*\}|\\newcolumntype\{[^}]*\}\{[^}]*\})' || true)

# --- Extract renewcommand stubs ---
RENEW_STUBS=$(echo "$PREAMBLE" | grep -oP '\\renewcommand\*?\{[^}]+\}' | sed 's/\\renewcommand\*?/\\providecommand/' | sed 's/$/{}/' || true)

# --- Also extract body-level macros (before the table) ---
BODY_MACROS=$(python3 -c "
import re, sys

text = open('$PAPER_TEX', 'r').read()

# Find the Nth table environment
pattern = re.compile(r'\\\\begin\{table\*?\}(?:\[[^\]]*\])?', re.DOTALL)
matches = list(pattern.finditer(text))

if $TAB_INDEX >= len(matches):
    print(f'ERROR: Only {len(matches)} tables found (index $TAB_INDEX out of range)', file=sys.stderr)
    sys.exit(1)

m = matches[$TAB_INDEX]
body_before = text[:m.start()]

# Find \begin{document} position
doc_start = text.find(r'\begin{document}')
if doc_start == -1:
    doc_start = 0

after_doc = body_before[doc_start:]

# Extract newcommand/definecolor/newcolumntable from body before this table
macros = []
for mc in re.finditer(r'\\newcommand\*?\{[^}]*\}\{[^}]*\}', after_doc, re.DOTALL):
    macros.append(mc.group(0))
for mc in re.finditer(r'\\def\s*\\\\[a-zA-Z@]+\{[^}]*\}', after_doc):
    macros.append(mc.group(0))
for mc in re.finditer(r'\\definecolor\{[^}]*\}\{[^}]*\}\{[^}]*\}', after_doc):
    macros.append(mc.group(0))
for mc in re.finditer(r'\\newcolumntype\{[^}]*\}\{[^}]*\}', after_doc):
    macros.append(mc.group(0))
for mc in re.finditer(r'\\pgfplotsset\{[^}]*\}', after_doc):
    macros.append(mc.group(0))
for mc in re.finditer(r'\\tikzset\{[^}]*\}', after_doc):
    macros.append(mc.group(0))

print('\n'.join(macros))
" 2>&1)

if [ $? -ne 0 ]; then
    echo "$BODY_MACROS"
    exit 1
fi

# --- Extract the Nth table body ---
TAB_BODY=$(python3 -c "
import re, sys

text = open('$PAPER_TEX', 'r').read()

# Find all \begin{table}...\end{table} environments
pattern = re.compile(r'\\\\begin\{table(\*?)\}(?:\[[^\]]*\])?', re.DOTALL)
matches = list(pattern.finditer(text))

if $TAB_INDEX >= len(matches):
    print(f'ERROR: Only {len(matches)} tables found (index $TAB_INDEX out of range)', file=sys.stderr)
    sys.exit(1)

m = matches[$TAB_INDEX]
star = m.group(1)
env_start = '\\\\begin{table' + star + '}'
env_end = '\\\\end{table' + star + '}'

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

# Strip \caption, \label, \vspace, negative \hspace
body = re.sub(r'\\\\caption\*?(?:\[[^\]]*\])?', '', body)
body = re.sub(r'\\\\label\{[^}]*\}', '', body)
body = re.sub(r'\\\\vspace\*?\{[^}]*\}', '', body)
body = re.sub(r'\\\\hspace\*?\{-[^}]*\}', '', body)

# Strip resizebox/scalebox/adjustbox wrappers
def remove_command(text, cmd):
    needle = '\\\\' + cmd
    while True:
        idx = text.find(needle)
        if idx == -1:
            break
        j = idx + len(needle)
        # skip optional [...]
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == '[':
            depth2 = 1
            j += 1
            while j < len(text) and depth2 > 0:
                if text[j] == '[': depth2 += 1
                elif text[j] == ']': depth2 -= 1
                j += 1
        # skip mandatory {...} args
        for _ in range(3):  # resizebox has up to 3 args
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and text[j] == '{':
                depth2 = 1
                j += 1
                while j < len(text) and depth2 > 0:
                    if text[j] == '{': depth2 += 1
                    elif text[j] == '}': depth2 -= 1
                    j += 1
        text = text[:idx] + text[j:]
    return text

body = remove_command(body, 'resizebox')
body = remove_command(body, 'scalebox')
body = remove_command(body, 'adjustbox')

# Strip \begin{center}/\end{center}/\centering
body = re.sub(r'\\\\begin\{center\}', '', body)
body = re.sub(r'\\\\end\{center\}', '', body)
body = re.sub(r'\\\\centering', '', body)

# Collapse repeated blank lines
body = re.sub(r'\n[ \t]*\n', '\n', body)

print(body.strip())
")

if [ $? -ne 0 ]; then
    echo "$TAB_BODY"
    exit 1
fi

# --- Build standalone LaTeX document ---
cat > "$OUT_DIR/table.tex" << 'TEXEOF'
\documentclass[border=6pt]{standalone}
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
\usepackage{pgfplotstable}
\usepackage{siunitx}
\usepackage{xspace}
\setlength{\textwidth}{6.50in}
\setlength{\columnwidth}{\textwidth}
\setlength{\linewidth}{\textwidth}
TEXEOF

# Append renew stubs
echo "$RENEW_STUBS" >> "$OUT_DIR/table.tex"

# Append preamble macros
echo "$PREAMBLE_MACROS" >> "$OUT_DIR/table.tex"

# Append body macros
echo "$BODY_MACROS" >> "$OUT_DIR/table.tex"

# Stubs for common citation/icon commands
cat >> "$OUT_DIR/table.tex" << 'TEXEOF'
\providecommand{\parencite}[1]{[#1]}
\providecommand{\citep}[1]{[#1]}
\providecommand{\citet}[1]{#1}
\providecommand{\faGithub}{}
\providecommand{\faEnvelopeO}{}
\begin{document}
\begin{minipage}{\textwidth}
TEXEOF

echo "$TAB_BODY" >> "$OUT_DIR/table.tex"

cat >> "$OUT_DIR/table.tex" << 'TEXEOF'
\end{minipage}
\end{document}
TEXEOF

echo "--- standalone LaTeX written to $OUT_DIR/table.tex ---"

# --- Compile ---
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

cp "$OUT_DIR/table.tex" "$TMPDIR/table.tex"
if [ -d "$SOURCE_DIR" ]; then
    cp -r "$SOURCE_DIR"/* "$TMPDIR/" 2>/dev/null || true
fi

echo "--- compiling with pdflatex ---"
cd "$TMPDIR"
if pdflatex -shell-escape -interaction=nonstopmode -halt-on-error table.tex > "$OUT_DIR/table.stdout" 2> "$OUT_DIR/table.stderr"; then
    echo "pdflatex OK"
else
    echo "pdflatex FAILED (see $OUT_DIR/table.stdout and $OUT_DIR/table.stderr)"
    if [ -f table.log ]; then
        cp table.log "$OUT_DIR/table.log"
        echo "--- log excerpt ---"
        grep -E '^!|Error|Warning' table.log | head -20 || true
    fi
    exit 1
fi

# --- Convert PDF to PNG ---
if [ -f table.pdf ]; then
    python3 -c "
import fitz
doc = fitz.open('table.pdf')
if len(doc) > 0:
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
    pix.save('$OUT_DIR/table.png')
    print(f'PNG saved to $OUT_DIR/table.png ({pix.width}x{pix.height})')
else:
    print('ERROR: PDF has no pages')
doc.close()
"
else
    echo "ERROR: table.pdf not generated"
    exit 1
fi

echo ""
echo "=== Done ==="
echo "Output files in $OUT_DIR/:"
ls -la "$OUT_DIR/"
echo ""
echo "PNG: $OUT_DIR/table.png"

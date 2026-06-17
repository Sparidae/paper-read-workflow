# LaTeX Failure Patterns

Common LaTeX rendering failures in `figure_extractor.py` and `table_extractor.py`, their symptoms, root causes, and fixes.

## Quick Diagnostic Table

| Symptom | Likely Cause | Fix Strategy |
|---------|-------------|--------------|
| Figure PNG is blank/white | `\includegraphics` path not resolved; or PDF image not converted | Check `figures/` dir has the file; run `convert_pdf_figures()` |
| Table rendered as matplotlib (degraded) | pdflatex compilation failed; fell back to matplotlib | Check `debug/<table>.log` for LaTeX errors |
| `! Undefined control sequence` in .log | Custom macro not injected into preamble | Add to `_prepare_figure_body` or inject preamble macros |
| `! LaTeX Error: File '...' not found` | Missing package or external data file | Install texlive package or inline the data |
| TikZ figure clipped at edges | Textwidth detection wrong for this document class | Add document class to `_detect_textwidth` heuristics |
| `\resizebox` wrapping causes blank output | Standalone class can't handle resizebox | Strip `\resizebox`/`\scalebox`/`\adjustbox` in `_prepare_figure_body` |
| Multi-figure environment shows only first figure | `\subfigure` environment not handled | Flag as LaTeX-drawn, compile whole body |
| pgfplotstable: data file not found | External `.dat`/`.csv` file referenced by relative path | `_inline_pgf_data()` should find and inline — check path resolution |
| Table: `! Extra alignment tab` | Column count mismatch in tabular | Manual fix — the source is malformed |
| `\caption` appears inside rendered figure | `_prepare_figure_body` didn't strip all caption variants | Add variant to the strip regex |
| Font warning: "Some font shapes were not available" | Font package uses non-standard encoding | Usually harmless; if it breaks output, map to available font |
| Figure renders but touches border at 8px margin | `_image_touches_border` retry logic kicking in | Increase text height or check page geometry |

## Debugging Workflow

### 1. Find the Debug Artifacts

After a failed `paper-tool add`, check:
```
papers/<paper_dir>/debug/
├── fig_001.tex        # Standalone LaTeX for this figure
├── fig_001.log        # pdflatex log output
├── fig_001.stdout     # pdflatex stdout
├── fig_001.stderr     # pdflatex stderr
├── fig_001.json       # Render status (width, backend, errors)
├── tab_001.tex        # Same structure for tables
├── tab_001.log
└── ...
```

### 2. Reproduce in Isolation

```bash
uv run skill/scripts/debug_render.py <paper-dir> --type figure --index <N>
```

This re-renders figure N from the paper directory in isolation.

### 3. Identify the Root Cause

- `.log` file → search for `!` (errors) and `Warning` (warnings)
- `.stderr` → pdflatex stderr output
- Compare `debug/fig_NNN.tex` preamble with the paper's actual preamble — are macros missing?

### 4. Common Fixes by Category

#### Missing Macros from Body

The figure/table extractor scans for `\newcommand`/`\definecolor` in the document body before each environment, but it may miss:
- `\def` macros
- `\newcolumntype` in table preamble
- `\pgfplotsset` in body text
- `\tikzset` in body text

**Fix**: Add the missing macro type to the preamble extraction logic in the extractor.

#### Missing Macros from Preamble

`_expand_tex_includes` merges `\input`/`\include` files, but custom `.sty` files and `\usepackage` with options may carry behavior not captured.

**Fix**: Add `\providecommand` stubs for the missing macros (see table_extractor.py's approach).

#### Textwidth Detection Failures

`_detect_textwidth` probes pdflatex or falls back to heuristics based on document class and known style packages (NeurIPS, ICLR, ICML, ACL, AAAI).

**Fix**: Add the new document class/style package to the heuristic table in `_detect_textwidth()`.

#### pgfplotstable Data Inlining

`_inline_pgf_data()` resolves data file paths relative to the source directory. If the path in `\pgfplotstableread{data.dat}` is non-standard (absolute, parent dir), resolution may fail.

**Fix**: Check `source/` directory structure, find the data file, adjust path resolution.

#### Image File Resolution

`parse_figures` resolves `\includegraphics` filenames by stem-matching against `figures/` (flattened from subdirs during extraction). If the filename uses extensions or subdirectories, matching may fail.

**Fix**: Check `figures/` for the actual filename, add resolution logic.

## Regression Verification Checklist

After any parser change, verify these cases haven't regressed:

1. **Simple includegraphics figure** — standard arxiv paper with PNG/PDF figures
2. **TikZ figure** — paper with `\begin{tikzpicture}` in figure environment
3. **PGFPlots figure** — paper with `\begin{axis}` in figure environment
4. **Multi-figure/subfigure** — paper with multiple `\includegraphics` in one figure
5. **Standard tabular table** — paper with `\begin{tabular}` in table environment
6. **Resizebox-wrapped table** — paper with `\resizebox{\textwidth}{!}{...}` wrapping tabular
7. **pgfplotstable table** — paper with `\pgfplotstabletypeset` or `\pgfplotstableread`
8. **NeurIPS/ICML/ICLR/ACL format** — verify textwidth detection per conference

Run each with:
```bash
uv run paper-tool add <known_good_url> --force --rerender-figures --rerender-tables
```

## Known Historical Fixes

Refer to `git log --oneline` for specific commits:
- `6f7707c` — body-internal macro definitions not being injected, causing table LaTeX compilation to degrade to matplotlib
- Check history for other parser-related fixes

---

## Render Repair Loop

`skill/scripts/extract_visuals.py` now supports an optional **check → diagnose → repair → re-render** loop (`--repair`). The loop is implemented in `skill/scripts/_render_repair.py` and used by both `_figure_extractor.py` and `_table_extractor.py`.

### What triggers a repair

A render is considered "bad" when any of the following is true:

- No PNG output was produced.
- A table fell back to the `matplotlib` renderer (LaTeX compile failed).
- The image content touches the border (likely clipped).
- The output is blank/white, extremely tiny, huge, or has an extreme aspect ratio.

### How the loop works

1. Compile the standalone LaTeX source.
2. Run `check_render_quality()` on the resulting PNG.
3. If quality is OK, stop.
4. Otherwise, read the pdflatex log and call `apply_rule_fix()`.
5. Rule fixes are applied one at a time, in order:
   - missing packages → add `\usepackage{pkg}`
   - undefined commands → add `\providecommand{\cmd}{...}`
   - missing external files → stub out the loading command
   - visual clipping → increase `\textheight` or add `\standaloneconfig`
   - blank/extreme tables → strip `\resizebox` / `\scalebox` / `\adjustbox` / `center` / `\centering`
6. If rule fixes are exhausted and `enable_llm_render_repair` is true, call an LLM with the failing source + log and use the corrected source it returns.
7. Re-render and re-check. Repeat up to `repair_max_attempts` times.

Every attempt writes debug artifacts:

```
papers/<paper_dir>/debug/
├── <stem>.attempt_1.latex.tex
├── <stem>.attempt_1.latex.log
├── <stem>.attempt_1.quality.json
├── <stem>.attempt_2.latex.tex
└── ...
```

### Enabling the loop

CLI:

```bash
uv run skill/scripts/extract_visuals.py <paper-dir> --rerender --repair
uv run skill/scripts/extract_visuals.py <paper-dir> --rerender --repair --enable-llm-repair
```

Config (`config.yaml`):

```yaml
llm:
  enable_render_repair: true
  enable_llm_render_repair: false
  repair_max_attempts: 3
  repair_model: ""
  repair_max_output_tokens: 16000
```

### Adding new rule fixes

To add a new deterministic fix:

1. Open `skill/scripts/_render_repair.py`.
2. Add the package/command/regex to the appropriate catalog (`_KNOWN_PACKAGES`, `_SAFE_STUBS`, or a new helper).
3. Call the helper inside `apply_rule_fix()` with a unique `previous_fixes` key.
4. Update this document with the new pattern.

Rule fixes are preferred over LLM fixes because they are fast, deterministic, and do not consume API quota.

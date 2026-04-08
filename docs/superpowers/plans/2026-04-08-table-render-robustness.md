# Table Rendering Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make table extraction prefer faithful PDF cropping, keep captions as separate text for translation/Notion upload, and reliably generate all table images for paper 2603.08706.

**Architecture:** Keep the existing `parse_tables()` entrypoint and `FigureInfo` output contract so the pipeline and Notion flow stay intact. Replace the default table image backend with a PDF-first cropper that uses lightweight PyMuPDF geometry/text heuristics, then fall back to a simpler semantic redraw only when the crop fails quality checks.

**Tech Stack:** Python 3.12, PyMuPDF (`fitz`), Pillow, existing pipeline/Notion integration, `uv`, ruff.

---

## File map

- Modify: `src/paper_tool/table_extractor.py`
  - Add PDF-first crop pipeline.
  - Keep caption extraction.
  - Keep a single redraw fallback.
  - Emit backend/debug status for each table.
- Modify: `src/paper_tool/models.py`
  - Extend `FigureInfo` only if the crop pipeline needs extra metadata.
- Modify: `src/paper_tool/pipeline.py`
  - Only if needed to pass PDF path or preserve table caption ordering.
- Modify: `src/paper_tool/notion_service.py`
  - Only if needed to ensure table image + translated caption are written as separate blocks in the right order.
- Create: `docs/superpowers/plans/2026-04-08-table-render-robustness.md`
- Verify against: `papers/2603.08706_Agentic_Critical_Training/`

## Task 1: Baseline the current table pipeline on paper 2603.08706

**Files:**
- Modify: none
- Verify: `papers/2603.08706_Agentic_Critical_Training/tables/`

- [ ] **Step 1: Run the current pipeline for the target paper with table rerendering enabled**

```bash
uv run python - <<'PY'
from pathlib import Path
from paper_tool.table_extractor import parse_tables

tex_path = Path("papers/2603.08706_Agentic_Critical_Training/merge.tex")
tables_dir = Path("papers/2603.08706_Agentic_Critical_Training/tables")
items = parse_tables(tex_path, tables_dir, max_tables=20, force_rerender=True)
print([(item.number, item.render_backend, item.image_path.name, item.caption[:80]) for item in items])
PY
```

Expected: table PNGs are regenerated and at least one table shows the current style problem.

- [ ] **Step 2: Inspect debug status for each generated table**

```bash
for f in papers/2603.08706_Agentic_Critical_Training/tables/debug/*.status.txt; do
  echo "== $f =="
  cat "$f"
done
```

Expected: every table has a renderer status so later comparisons are easy.

- [ ] **Step 3: Confirm where the pipeline gets table metadata from**

```python
# Keep this contract stable while changing internals.
FigureInfo(
    image_path=output_path,
    caption=caption,
    label=label,
    number=tbl_number,
    kind="table",
    render_backend=render_backend,
)
```

Expected: no caller changes are required unless the crop path needs extra metadata.

- [ ] **Step 4: Commit the baseline notes only if code changed**

```bash
# No commit if this task is read-only.
```

## Task 2: Add PDF-first table crop helpers in `table_extractor.py`

**Files:**
- Modify: `src/paper_tool/table_extractor.py`

- [ ] **Step 1: Add a helper to locate the sibling paper PDF from `merge.tex`**

```python
def _resolve_pdf_path(tex_path: Path) -> Path | None:
    for name in ("paper.pdf", f"{tex_path.parent.name}.pdf"):
        candidate = tex_path.parent / name
        if candidate.exists():
            return candidate
    pdfs = sorted(tex_path.parent.glob("*.pdf"))
    return pdfs[0] if pdfs else None
```

- [ ] **Step 2: Add caption normalization and matching helpers**

```python
def _normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().lower()
    return re.sub(r"[^\w\s.%()-]", "", text)


def _caption_keywords(caption: str, limit: int = 8) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z0-9%.-]+", _normalize_text(caption)) if len(w) >= 3]
    return words[:limit]
```
```

- [ ] **Step 3: Add a helper to find the PDF page and caption block for one table**

```python
def _find_caption_block(doc, table_number: int, caption: str):
    needle = f"table {table_number}"
    keywords = _caption_keywords(caption)
    best = None
    for page_index, page in enumerate(doc):
        blocks = page.get_text("blocks")
        for block in blocks:
            x0, y0, x1, y1, text, *_ = block
            norm = _normalize_text(text)
            score = 0
            if needle in norm:
                score += 10
            score += sum(1 for kw in keywords if kw in norm)
            if score and (best is None or score > best[0]):
                best = (score, page_index, (x0, y0, x1, y1), text)
    return best
```

- [ ] **Step 4: Add a helper to estimate the table body box near the caption**

```python
def _estimate_table_crop(page, caption_rect, *, search_above: bool = True):
    import fitz

    drawings = page.get_drawings()
    text_blocks = page.get_text("blocks")
    page_rect = page.rect
    cy0, cy1 = caption_rect.y0, caption_rect.y1
    vertical_window = fitz.Rect(0, max(0, cy0 - page_rect.height * 0.55), page_rect.width, cy0) if search_above else fitz.Rect(0, cy1, page_rect.width, min(page_rect.height, cy1 + page_rect.height * 0.55))

    rects = []
    for item in drawings:
        rect = item.get("rect")
        if rect and rect.intersects(vertical_window):
            rects.append(rect)
    for x0, y0, x1, y1, text, *_ in text_blocks:
        rect = fitz.Rect(x0, y0, x1, y1)
        if rect.intersects(vertical_window) and _normalize_text(text):
            rects.append(rect)
    if not rects:
        return None
    crop = fitz.Rect(rects[0])
    for rect in rects[1:]:
        crop |= rect
    return crop
```

- [ ] **Step 5: Add a helper to trim away the caption area and clamp margins**

```python
def _refine_crop_rect(page, crop_rect, caption_rect):
    import fitz

    rect = fitz.Rect(crop_rect)
    if rect.intersects(caption_rect):
        if caption_rect.y0 >= rect.y0:
            rect.y1 = min(rect.y1, caption_rect.y0 - 2)
        else:
            rect.y0 = max(rect.y0, caption_rect.y1 + 2)
    rect.x0 = max(page.rect.x0, rect.x0 - 6)
    rect.y0 = max(page.rect.y0, rect.y0 - 6)
    rect.x1 = min(page.rect.x1, rect.x1 + 6)
    rect.y1 = min(page.rect.y1, rect.y1 + 6)
    return rect if rect.width > 40 and rect.height > 40 else None
```

- [ ] **Step 6: Add a helper to render a crop rect to PNG**

```python
def _render_pdf_crop(page, crop_rect, output_path: Path, zoom: float = 3.0) -> bool:
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=crop_rect, alpha=False)
    if pix.width < 80 or pix.height < 80:
        return False
    pix.save(str(output_path))
    return True
```

## Task 3: Add crop quality checks and a single redraw fallback

**Files:**
- Modify: `src/paper_tool/table_extractor.py`

- [ ] **Step 1: Add a lightweight crop validator**

```python
def _looks_like_valid_table_image(image_path: Path) -> bool:
    from PIL import Image
    import numpy as np

    img = Image.open(str(image_path)).convert("L")
    arr = np.array(img)
    if arr.size == 0:
        return False
    dark_ratio = float((arr < 245).mean())
    return 0.01 <= dark_ratio <= 0.60
```

- [ ] **Step 2: Add a wrapper that runs PDF-first crop for one table**

```python
def _render_table_from_pdf(pdf_path: Path, table_number: int, caption: str, output_path: Path, debug_dir: Path | None = None, stem: str = "table") -> bool:
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        match = _find_caption_block(doc, table_number, caption)
        if not match:
            if debug_dir is not None:
                _write_status(debug_dir, stem, renderer="pdf_crop_failed", note="caption_not_found")
            return False
        _, page_index, rect_tuple, _ = match
        page = doc[page_index]
        caption_rect = fitz.Rect(rect_tuple)
        crop_rect = _estimate_table_crop(page, caption_rect, search_above=True)
        if crop_rect is None:
            crop_rect = _estimate_table_crop(page, caption_rect, search_above=False)
        if crop_rect is None:
            if debug_dir is not None:
                _write_status(debug_dir, stem, renderer="pdf_crop_failed", note="table_region_not_found")
            return False
        crop_rect = _refine_crop_rect(page, crop_rect, caption_rect)
        if crop_rect is None or not _render_pdf_crop(page, crop_rect, output_path):
            if debug_dir is not None:
                _write_status(debug_dir, stem, renderer="pdf_crop_failed", note="invalid_crop_rect")
            return False
    if not _looks_like_valid_table_image(output_path):
        output_path.unlink(missing_ok=True)
        if debug_dir is not None:
            _write_status(debug_dir, stem, renderer="pdf_crop_failed", note="image_quality_check_failed")
        return False
    _trim_whitespace(output_path, padding=8)
    if debug_dir is not None:
        _write_status(debug_dir, stem, renderer="pdf_crop")
    return True
```

- [ ] **Step 3: Make the redraw fallback more neutral than the current style**

```python
# Keep matplotlib fallback, but simplify style:
# - white background only
# - no gray header fill
# - slightly larger font
# - stronger row spacing
# - keep booktabs lines only
```

- [ ] **Step 4: Keep fallback scope narrow**

```python
# Do not add OCR, CV models, or extra extractors.
# The only backends should be: pdf_crop -> latex -> matplotlib (or pdf_crop -> matplotlib if latex no longer helps).
```

## Task 4: Wire the new backend into `parse_tables()` with minimal surface change

**Files:**
- Modify: `src/paper_tool/table_extractor.py`
- Modify: `src/paper_tool/pipeline.py` (only if needed)

- [ ] **Step 1: Resolve PDF once near the top of `parse_tables()`**

```python
pdf_path = _resolve_pdf_path(tex_path)
```

- [ ] **Step 2: Replace the current default renderer order**

```python
ok = False
if pdf_path is not None:
    ok = _render_table_from_pdf(
        pdf_path,
        tbl_number,
        caption,
        output_path,
        debug_dir=debug_dir,
        stem=stem,
    )
    if ok:
        render_backend = "pdf_crop"

if not ok:
    ok = _render_table_latex(
        table_body or tabular_raw,
        output_path,
        preamble_macros,
        renew_stubs=renew_stubs,
        text_width=text_width,
        debug_dir=debug_dir,
        stem=stem,
    )
    if ok:
        render_backend = "latex"

if not ok:
    rows = _parse_tabular_rows(env_text)
    ok = _render_table_matplotlib(rows, output_path, debug_dir=debug_dir, stem=stem)
    if ok:
        render_backend = "matplotlib"
```

- [ ] **Step 3: Preserve the existing `FigureInfo` contract**

```python
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
```

- [ ] **Step 4: Only touch pipeline/Notion code if the contract breaks**

```python
# Preferred outcome: no caller changes.
```

## Task 5: Verify on paper 2603.08706 and tune heuristics

**Files:**
- Modify: `src/paper_tool/table_extractor.py`
- Verify: `papers/2603.08706_Agentic_Critical_Training/tables/`

- [ ] **Step 1: Regenerate all tables after the PDF-first change**

```bash
uv run python - <<'PY'
from pathlib import Path
from paper_tool.table_extractor import parse_tables

tex_path = Path("papers/2603.08706_Agentic_Critical_Training/merge.tex")
tables_dir = Path("papers/2603.08706_Agentic_Critical_Training/tables")
items = parse_tables(tex_path, tables_dir, max_tables=20, force_rerender=True)
for item in items:
    print(item.number, item.render_backend, item.image_path)
PY
```

Expected: all tables for this paper are regenerated with backend labels.

- [ ] **Step 2: Inspect the target outputs, especially Table 3**

```bash
ls papers/2603.08706_Agentic_Critical_Training/tables/*.png
```

Expected: every table PNG exists and Table 3 is visibly closer to the paper than the old redraw style.

- [ ] **Step 3: Inspect debug files if any table used fallback**

```bash
for f in papers/2603.08706_Agentic_Critical_Training/tables/debug/*.status.txt; do
  echo "== $f =="
  cat "$f"
done
```

Expected: fallback reasons are explicit enough to tune one heuristic at a time.

- [ ] **Step 4: Tune only the crop heuristics needed for this paper**

```python
# Adjust search window, padding, caption exclusion, or quality thresholds.
# Do not add new extraction subsystems.
```

## Task 6: Verify style/format and prepare the final artifact list

**Files:**
- Modify: `src/paper_tool/table_extractor.py`
- Verify: generated PNGs and debug files

- [ ] **Step 1: Format the Python changes**

```bash
uv run ruff format src/paper_tool/table_extractor.py src/paper_tool/models.py src/paper_tool/pipeline.py src/paper_tool/notion_service.py
```

Expected: files are reformatted or left unchanged.

- [ ] **Step 2: Run ruff fixes on touched files**

```bash
uv run ruff check --fix src/paper_tool/table_extractor.py src/paper_tool/models.py src/paper_tool/pipeline.py src/paper_tool/notion_service.py
```

Expected: no remaining lint issues in touched files.

- [ ] **Step 3: Re-run the target paper extraction one final time**

```bash
uv run python - <<'PY'
from pathlib import Path
from paper_tool.table_extractor import parse_tables

tex_path = Path("papers/2603.08706_Agentic_Critical_Training/merge.tex")
tables_dir = Path("papers/2603.08706_Agentic_Critical_Training/tables")
items = parse_tables(tex_path, tables_dir, max_tables=20, force_rerender=True)
print("count=", len(items))
for item in items:
    print(item.number, item.render_backend, item.image_path.name, item.caption[:100])
PY
```

Expected: stable table list and output paths for the user.

- [ ] **Step 4: Commit the implementation**

```bash
git add docs/superpowers/plans/2026-04-08-table-render-robustness.md src/paper_tool/table_extractor.py src/paper_tool/models.py src/paper_tool/pipeline.py src/paper_tool/notion_service.py papers/2603.08706_Agentic_Critical_Training/tables
git commit -m "[fix]改进表格图片提取与渲染"
```

Expected: one commit containing the plan and implementation.

## Self-review checklist

- Spec coverage: PDF-first crop, separate caption flow, simple fallback, and verification on paper 2603.08706 are all covered by Tasks 2-6.
- Placeholder scan: no TODO/TBD placeholders remain.
- Type consistency: `parse_tables()` still returns `FigureInfo` with `render_backend` populated.

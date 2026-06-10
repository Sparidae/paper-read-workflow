# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pymupdf>=1.24.0",
#     "matplotlib>=3.7.0",
#     "pylatexenc>=2.10",
#     "pyyaml>=6.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""Extract and render figures + tables from LaTeX source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _figure_extractor import convert_pdf_figures, parse_figures
from _lib import load_config, output_error, output_ok
from _table_extractor import parse_tables


def main():
    parser = argparse.ArgumentParser(
        description="Extract figures and tables from paper LaTeX source"
    )
    parser.add_argument(
        "paper_dir", help="Path to paper directory (must contain paper.tex)"
    )
    parser.add_argument(
        "--max-figures", type=int, default=None, help="Max figures to extract"
    )
    parser.add_argument(
        "--max-tables", type=int, default=None, help="Max tables to extract"
    )
    parser.add_argument(
        "--rerender",
        action="store_true",
        help="Force re-render even if cached PNGs exist",
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    if not paper_dir.is_dir():
        output_error(f"Paper directory not found: {paper_dir}")
        return

    tex_path = paper_dir / "paper.tex"
    if not tex_path.exists():
        output_error("No paper.tex found — LaTeX source required for visual extraction")
        return

    config = load_config()
    llm_config = config.get("llm", {})
    max_figures = args.max_figures or llm_config.get("max_figures", 15)
    max_tables = args.max_tables or llm_config.get("max_tables", 10)

    figures_dir = paper_dir / "figures"
    tables_dir = paper_dir / "tables"
    figures_dir.mkdir(exist_ok=True)
    tables_dir.mkdir(exist_ok=True)

    convert_pdf_figures(figures_dir)

    figures = parse_figures(
        tex_path=tex_path,
        figures_dir=figures_dir,
        max_figures=max_figures,
        force_rerender=args.rerender,
    )

    tables = parse_tables(
        tex_path=tex_path,
        tables_dir=tables_dir,
        max_tables=max_tables,
        force_rerender=args.rerender,
    )

    visuals_data = []
    for fig in figures:
        visuals_data.append(
            {
                "image_path": str(fig.image_path),
                "caption": fig.caption,
                "label": fig.label,
                "number": fig.number,
                "kind": fig.kind,
                "render_backend": fig.render_backend,
            }
        )
    for tbl in tables:
        visuals_data.append(
            {
                "image_path": str(tbl.image_path),
                "caption": tbl.caption,
                "label": tbl.label,
                "number": tbl.number,
                "kind": tbl.kind,
                "render_backend": tbl.render_backend,
            }
        )

    visuals_path = paper_dir / "visuals.json"
    visuals_path.write_text(json.dumps(visuals_data, ensure_ascii=False, indent=2))

    tables_latex = sum(1 for t in tables if t.render_backend == "latex")
    tables_matplotlib = sum(1 for t in tables if t.render_backend == "matplotlib")

    output_ok(
        f"Extracted {len(figures)} figures, {len(tables)} tables",
        figures_count=len(figures),
        tables_count=len(tables),
        render_stats={
            "tables_latex": tables_latex,
            "tables_matplotlib": tables_matplotlib,
        },
        outputs={"visuals": str(visuals_path)},
    )


if __name__ == "__main__":
    main()

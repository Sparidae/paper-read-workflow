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
"""Re-render a single figure or table from a paper's LaTeX source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _figure_extractor import parse_figures
from _lib import output_error, output_ok
from _table_extractor import parse_tables


def main():
    parser = argparse.ArgumentParser(description="Re-render a single figure or table")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument(
        "--type",
        choices=["figure", "table"],
        required=True,
        help="Type of visual to re-render",
    )
    parser.add_argument(
        "--index", type=int, required=True, help="1-based index of the visual"
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    tex_path = paper_dir / "paper.tex"
    if not tex_path.exists():
        output_error("No paper.tex found — LaTeX source required")
        return

    if args.type == "figure":
        figures_dir = paper_dir / "figures"
        figures_dir.mkdir(exist_ok=True)
        results = parse_figures(
            tex_path=tex_path,
            figures_dir=figures_dir,
            max_figures=999,
            force_rerender=True,
        )
        match = [f for f in results if f.number == args.index]
        if not match:
            output_error(
                f"Figure {args.index} not found (available: {[f.number for f in results]})"
            )
            return
        fig = match[0]
        output_ok(
            f"Re-rendered figure {fig.number}",
            image_path=str(fig.image_path),
            caption=fig.caption,
            render_backend=fig.render_backend,
        )
    else:
        tables_dir = paper_dir / "tables"
        tables_dir.mkdir(exist_ok=True)
        results = parse_tables(
            tex_path=tex_path,
            tables_dir=tables_dir,
            max_tables=999,
            force_rerender=True,
        )
        match = [t for t in results if t.number == args.index]
        if not match:
            output_error(
                f"Table {args.index} not found (available: {[t.number for t in results]})"
            )
            return
        tbl = match[0]
        output_ok(
            f"Re-rendered table {tbl.number}",
            image_path=str(tbl.image_path),
            caption=tbl.caption,
            render_backend=tbl.render_backend,
        )

    # Update visuals.json if it exists
    visuals_path = paper_dir / "visuals.json"
    if visuals_path.exists():
        visuals = json.loads(visuals_path.read_text())
        target = match[0]
        for v in visuals:
            if v.get("kind") == args.type and v.get("number") == args.index:
                v["image_path"] = str(target.image_path)
                v["render_backend"] = target.render_backend
                break
        visuals_path.write_text(json.dumps(visuals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

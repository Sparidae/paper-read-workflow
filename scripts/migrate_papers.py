"""
Migrate papers/ from the old flat structure to per-paper subdirectories.

Old layout:
  papers/
    {id}_{title}.pdf
    {id}_{title}.tex          (optional)
    {id}_{title}_figures/     (optional)

New layout:
  papers/
    {id}_{title}/
      paper.pdf
      paper.tex               (optional)
      figures/                (optional)

Usage:
  uv run python scripts/migrate_papers.py [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PAPERS_DIR = Path(__file__).parent.parent / "papers"


def collect_groups(papers_dir: Path) -> dict[str, dict]:
    """
    Scan papers_dir for flat-structure files and group them by stem.
    Returns {stem: {"pdf": Path|None, "tex": Path|None, "figures": Path|None}}
    """
    groups: dict[str, dict] = {}

    def _get(stem: str) -> dict:
        if stem not in groups:
            groups[stem] = {"pdf": None, "tex": None, "figures": None}
        return groups[stem]

    for item in sorted(papers_dir.iterdir()):
        # Skip already-migrated subdirectories
        if item.is_dir() and not item.name.endswith("_figures"):
            continue

        if item.is_file() and item.suffix == ".pdf":
            stem = item.stem
            _get(stem)["pdf"] = item

        elif item.is_file() and item.suffix == ".tex":
            stem = item.stem
            _get(stem)["tex"] = item

        elif item.is_dir() and item.name.endswith("_figures"):
            # Strip _figures suffix to get the stem
            stem = item.name[: -len("_figures")]
            _get(stem)["figures"] = item

    # Only keep groups that have at least a PDF
    return {stem: info for stem, info in groups.items() if info["pdf"] is not None}


def migrate(papers_dir: Path, dry_run: bool) -> None:
    tag = "[DRY RUN] " if dry_run else ""
    groups = collect_groups(papers_dir)

    if not groups:
        print("没有发现需要迁移的文件，退出。")
        return

    print(f"发现 {len(groups)} 篇论文需要迁移:\n")

    for stem, info in groups.items():
        paper_dir = papers_dir / stem
        print(f"  {stem}/")
        print(f"    {info['pdf'].name} → paper.pdf")
        if info["tex"]:
            print(f"    {info['tex'].name} → paper.tex")
        if info["figures"]:
            print(f"    {info['figures'].name}/ → figures/")
        print()

    if dry_run:
        print("（dry-run 模式，不执行实际操作）")
        return

    confirm = input("确认迁移？[y/N] ").strip().lower()
    if confirm != "y":
        print("已取消。")
        return

    print()
    ok = 0
    fail = 0

    for stem, info in groups.items():
        paper_dir = papers_dir / stem
        try:
            paper_dir.mkdir(exist_ok=True)

            shutil.move(str(info["pdf"]), str(paper_dir / "paper.pdf"))
            print(f"✓ {stem}/paper.pdf")

            if info["tex"]:
                shutil.move(str(info["tex"]), str(paper_dir / "paper.tex"))
                print(f"✓ {stem}/paper.tex")

            if info["figures"]:
                dest = paper_dir / "figures"
                if dest.exists():
                    # Merge: move individual files inside
                    for f in info["figures"].iterdir():
                        shutil.move(str(f), str(dest / f.name))
                    info["figures"].rmdir()
                else:
                    shutil.move(str(info["figures"]), str(dest))
                print(f"✓ {stem}/figures/")

            ok += 1
        except Exception as e:
            print(f"✗ {stem}: {e}", file=sys.stderr)
            fail += 1

    print(f"\n完成：成功 {ok} 篇，失败 {fail} 篇。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate papers to per-paper subdirs")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不执行")
    args = parser.parse_args()

    migrate(PAPERS_DIR, dry_run=args.dry_run)

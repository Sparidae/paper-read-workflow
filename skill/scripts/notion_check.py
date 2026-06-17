# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""Check if a paper URL already exists in the Notion database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import httpx
from _backend_config import BackendConfigError, load_notion_config
from _lib import output_error, output_ok


def _load_notion_config(interactive: bool = True) -> tuple[str, str, dict[str, str]]:
    """Return (token, database_id, properties) from backends/notion/backend.yaml."""
    cfg = load_notion_config(interactive=interactive)
    return (
        cfg["token"],
        cfg["database_id"],
        cfg["properties"],
    )


def find_existing_pages(
    token: str, database_id: str, url_prop: str, paper_url: str
) -> list[dict]:
    """Query Notion for non-archived pages matching the URL."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    pages = []
    next_cursor = None

    with httpx.Client(timeout=30) as client:
        while True:
            body: dict = {
                "filter": {
                    "property": url_prop,
                    "url": {"equals": paper_url},
                },
            }
            if next_cursor:
                body["start_cursor"] = next_cursor

            resp = client.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                if page.get("archived") or page.get("in_trash"):
                    continue
                pages.append(
                    {
                        "id": page["id"],
                        "url": f"https://www.notion.so/{page['id'].replace('-', '')}",
                    }
                )

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")

    return pages


def main():
    parser = argparse.ArgumentParser(description="Check if paper exists in Notion")
    parser.add_argument("paper_url", help="Paper URL to check")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt for missing config; emit a structured error",
    )
    args = parser.parse_args()

    try:
        token, database_id, properties = _load_notion_config(
            interactive=not args.non_interactive
        )
    except BackendConfigError as e:
        output_error(
            f"Notion backend needs configuration: {e}",
            backend=e.backend,
            missing=e.missing,
            hint="Run interactively or fill backends/notion/backend.yaml",
        )
        sys.exit(1)
    url_prop = properties.get("url", "论文链接")

    try:
        pages = find_existing_pages(token, database_id, url_prop, args.paper_url)
    except Exception as e:
        output_error(f"Notion query failed: {e}")
        return

    if pages:
        output_ok(
            f"Found {len(pages)} existing page(s)",
            exists=True,
            pages=pages,
        )
    else:
        output_ok("No existing page found", exists=False, pages=[])


if __name__ == "__main__":
    main()

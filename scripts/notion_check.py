# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27.0", "pyyaml>=6.0", "python-dotenv>=1.0.0"]
# ///
"""Check if a paper URL already exists in the Notion database."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import httpx
from _lib import load_config, output_error, output_ok


def _load_notion_config() -> tuple[str, str, dict[str, str]]:
    """Return (token, database_id, properties) from env + notion_schema.yaml."""
    import yaml

    load_config()  # ensures .env is loaded
    token = os.getenv("NOTION_TOKEN", "")
    database_id = os.getenv("NOTION_DATABASE_ID", "")

    if not token or not database_id:
        output_error("NOTION_TOKEN and NOTION_DATABASE_ID must be set in .env")
        sys.exit(1)

    from _lib import find_project_root

    schema_path = find_project_root() / "notion_schema.yaml"
    if schema_path.exists():
        with open(schema_path) as f:
            schema = yaml.safe_load(f) or {}
        properties = schema.get("notion", {}).get("properties", {})
    else:
        properties = {}

    return token, database_id, properties


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
    args = parser.parse_args()

    token, database_id, properties = _load_notion_config()
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

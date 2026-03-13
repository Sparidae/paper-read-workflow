"""Notion database integration: create pages and write structured notes."""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from paper_tool.config import get_config
from paper_tool.models import Classification, PaperMetadata, PaperNote


# Notion API: single rich_text block content limit is 2000 chars
_BLOCK_CHAR_LIMIT = 1900


def _chunk_text(text: str, limit: int = _BLOCK_CHAR_LIMIT) -> list[str]:
    """Split text into chunks that fit within Notion's block size limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content}}]


def _paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def _heading2_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _rich_text(text)},
    }


def _heading3_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": _rich_text(text)},
    }


def _bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(text)},
    }


def _divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _text_to_blocks(text: str) -> list[dict]:
    """Convert plain text into Notion paragraph blocks, respecting size limits."""
    blocks = []
    for chunk in _chunk_text(text):
        blocks.append(_paragraph_block(chunk))
    return blocks


def _freeform_to_blocks(text: str) -> list[dict]:
    """
    Convert freeform text (Markdown-ish) into Notion blocks.

    Supports:
      ## Heading  → heading_2
      ### Heading → heading_3
      - item      → bulleted_list_item
      blank line  → paragraph separator (skipped)
      other text  → paragraph (chunked if > limit)
    """
    blocks: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append(_heading3_block(stripped[4:].strip()))
        elif stripped.startswith("## "):
            blocks.append(_heading2_block(stripped[3:].strip()))
        elif stripped.startswith("# "):
            blocks.append(_heading2_block(stripped[2:].strip()))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            item = stripped[2:].strip()
            for chunk in _chunk_text(item):
                blocks.append(_bullet_block(chunk))
        else:
            for chunk in _chunk_text(stripped):
                blocks.append(_paragraph_block(chunk))
    return blocks


def _note_to_blocks(note: PaperNote) -> list[dict]:
    """Convert a PaperNote into a list of Notion blocks."""
    blocks: list[dict] = []

    sections: list[tuple[str, str | list[str]]] = [
        ("论文概述", note.overview),
        ("研究问题", note.research_problem),
        ("方法论", note.methodology),
        ("主要贡献", note.contributions),
        ("实验与结果", note.experiments),
        ("局限性与未来工作", note.limitations),
        ("关键要点", note.key_takeaways),
    ]

    for heading, content in sections:
        blocks.append(_heading2_block(heading))
        if isinstance(content, list):
            for item in content:
                for chunk in _chunk_text(item):
                    blocks.append(_bullet_block(chunk))
        else:
            blocks.extend(_text_to_blocks(content))

    return blocks


class NotionService:
    """Handles all Notion API interactions."""

    def __init__(self) -> None:
        from notion_client import Client

        cfg = get_config()
        self._client = Client(auth=cfg.notion_token)
        self._database_id = cfg.notion_database_id
        self._props = cfg.notion_properties
        self._status_type = cfg.notion_status_type
        self._default_status = cfg.notion_default_status
        self._paper_type_prop = cfg.notion_paper_type_prop
        self._institution_prop = cfg.notion_institution_prop

    def get_property_options(self, prop_name: str) -> list[str]:
        """Fetch existing option names for a select or multi_select property."""
        try:
            db = self._client.databases.retrieve(database_id=self._database_id)
            prop = db.get("properties", {}).get(prop_name, {})
            ptype = prop.get("type", "")
            if ptype in ("select", "multi_select"):
                return [o["name"] for o in prop.get(ptype, {}).get("options", [])]
        except Exception:
            pass
        return []

    def get_classification_options(self) -> dict[str, list[str]]:
        """Return all option lists needed by the LLM classifier in one API call."""
        try:
            db = self._client.databases.retrieve(database_id=self._database_id)
            props = db.get("properties", {})

            def _opts(prop_name: str) -> list[str]:
                p = props.get(prop_name, {})
                t = p.get("type", "")
                if t in ("select", "multi_select"):
                    return [o["name"] for o in p.get(t, {}).get("options", [])]
                return []

            return {
                "paper_type": _opts(self._paper_type_prop),
                "research_areas": _opts(self._props.get("tags", "")),
                "institutions": _opts(self._institution_prop),
            }
        except Exception:
            return {"paper_type": [], "research_areas": [], "institutions": []}

    def _rate_limit(self) -> None:
        """Respect Notion's ~3 req/s rate limit."""
        time.sleep(0.35)

    def find_existing_page(self, paper_url: str) -> str | None:
        """Return the page ID if a page with this URL already exists."""
        url_prop = self._props.get("url", "URL")
        try:
            results = self._client.databases.query(
                database_id=self._database_id,
                filter={
                    "property": url_prop,
                    "url": {"equals": paper_url},
                },
            )
            pages = results.get("results", [])
            if pages:
                return pages[0]["id"]
        except Exception:
            pass
        return None

    def create_page(self, metadata: PaperMetadata) -> str:
        """Create a new database page for the paper. Returns the page ID."""
        props = self._props
        today = datetime.now().date().isoformat()

        properties: dict[str, Any] = {
            props["title"]: {
                "title": [{"text": {"content": metadata.title}}]
            },
        }

        if metadata.authors:
            properties[props["authors"]] = {
                "rich_text": _rich_text(metadata.authors_str[:2000])
            }

        properties[props["source"]] = {
            "select": {"name": metadata.source.value}
        }

        properties[props["url"]] = {
            "url": metadata.url
        }

        if metadata.published_date:
            properties[props["published_date"]] = {
                "date": {"start": metadata.published_date.isoformat()}
            }

        properties[props["added_date"]] = {
            "date": {"start": today}
        }

        if self._status_type == "checkbox":
            # checkbox: false = unread (haven't finished reading)
            properties[props["status"]] = {"checkbox": False}
        else:
            properties[props["status"]] = {
                "select": {"name": self._default_status}
            }

        response = self._client.pages.create(
            parent={"database_id": self._database_id},
            properties=properties,
        )
        return response["id"]

    def update_summary(self, page_id: str, summary: str) -> None:
        """Write LLM-generated one-sentence summary to the abstract property."""
        prop_name = self._props.get("abstract", "")
        if not prop_name or not summary:
            return
        self._client.pages.update(
            page_id=page_id,
            properties={
                prop_name: {"rich_text": _rich_text(summary[:2000])}
            },
        )
        self._rate_limit()

    def update_classifications(self, page_id: str, classification: Classification) -> None:
        """Write classification tags (paper_type, research_areas, institutions) to page properties."""
        props = self._props
        prop_updates: dict[str, Any] = {}

        if classification.research_areas and props.get("tags"):
            prop_updates[props["tags"]] = {
                "multi_select": [{"name": v} for v in classification.research_areas]
            }
        if classification.paper_type and self._paper_type_prop:
            prop_updates[self._paper_type_prop] = {
                "multi_select": [{"name": v} for v in classification.paper_type]
            }
        if classification.institutions and self._institution_prop:
            prop_updates[self._institution_prop] = {
                "multi_select": [{"name": v} for v in classification.institutions]
            }

        if prop_updates:
            self._client.pages.update(page_id=page_id, properties=prop_updates)
            self._rate_limit()

    def append_note(self, page_id: str, note: PaperNote) -> None:
        """Append note content blocks to the page body."""
        if note.is_freeform:
            content_blocks = _freeform_to_blocks(note.raw_content or "")
        else:
            content_blocks = _note_to_blocks(note)
        blocks = [_divider_block(), _heading2_block("AI 分析笔记")] + content_blocks

        batch_size = 100
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i : i + batch_size]
            self._client.blocks.children.append(
                block_id=page_id,
                children=batch,
            )
            self._rate_limit()

    def get_page_url(self, page_id: str) -> str:
        """Return the Notion web URL for the page."""
        clean_id = page_id.replace("-", "")
        return f"https://www.notion.so/{clean_id}"

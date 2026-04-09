"""Notion database integration: create pages and write structured notes."""

from __future__ import annotations

import mimetypes
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from paper_tool.config import get_config
from paper_tool.models import Classification, FigureInfo, PaperMetadata, PaperNote

# Notion API: single rich_text element content limit is 2000 chars
_BLOCK_CHAR_LIMIT = 1900

# Notion file upload API (uses direct httpx, not notion-client)
_NOTION_VERSION = "2026-03-11"
_NOTION_API = "https://api.notion.com/v1"

# File upload API uses a different version header
_NOTION_API_VERSION = "2026-03-11"
_NOTION_API_BASE = "https://api.notion.com/v1"

# Inline Markdown pattern: links, bold+italic, bold, italic, inline code, inline math
_INLINE_MD = re.compile(
    r"\[([^\]\n]+)\]\(([^)\n]+)\)"  # [text](url)
    r"|\*\*\*([^*\n]+)\*\*\*"  # ***bold italic***
    r"|\*\*([^*\n]+)\*\*"  # **bold**
    r"|\*([^*\n]+)\*"  # *italic*
    r"|`([^`\n]+)`"  # `code`
    r"|\\\((.+?)\\\)"  # \(...\)  inline math
    r"|\$([^$\n]+)\$"  # $...$ inline math
)


def _text_obj(
    content: str,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
    url: str | None = None,
) -> dict:
    obj: dict = {
        "type": "text",
        "text": {"content": content},
        "annotations": {
            "bold": bold,
            "italic": italic,
            "strikethrough": False,
            "underline": False,
            "code": code,
            "color": "default",
        },
    }
    if url:
        obj["text"]["link"] = {"url": url}
    return obj


def _equation_inline_obj(expr: str) -> dict:
    """Notion inline equation rich_text object."""
    return {"type": "equation", "equation": {"expression": expr.strip()}}


def _parse_inline(text: str) -> list[dict]:
    """
    Parse inline Markdown into Notion rich_text objects.

    Handles: [text](url), ***bold italic***, **bold**, *italic*, `code`,
    \\(...\\) and $...$ inline math equations.
    Plain text segments are kept as-is. Each element respects the 2000-char limit.
    """
    result: list[dict] = []
    pos = 0

    for m in _INLINE_MD.finditer(text):
        # Plain text before this match
        before = text[pos : m.start()]
        if before:
            for chunk in _split_str(before):
                result.append(_text_obj(chunk))

        link_label, link_url = m.group(1), m.group(2)
        bold_italic = m.group(3)
        bold = m.group(4)
        italic = m.group(5)
        code = m.group(6)
        math_paren = m.group(7)  # \(...\)
        math_dollar = m.group(8)  # $...$

        if link_label is not None:
            for chunk in _split_str(link_label):
                result.append(_text_obj(chunk, url=link_url))
        elif bold_italic is not None:
            for chunk in _split_str(bold_italic):
                result.append(_text_obj(chunk, bold=True, italic=True))
        elif bold is not None:
            for chunk in _split_str(bold):
                result.append(_text_obj(chunk, bold=True))
        elif italic is not None:
            for chunk in _split_str(italic):
                result.append(_text_obj(chunk, italic=True))
        elif code is not None:
            for chunk in _split_str(code):
                result.append(_text_obj(chunk, code=True))
        elif math_paren is not None:
            result.append(_equation_inline_obj(math_paren))
        elif math_dollar is not None:
            result.append(_equation_inline_obj(math_dollar))

        pos = m.end()

    # Remaining plain text
    tail = text[pos:]
    if tail:
        for chunk in _split_str(tail):
            result.append(_text_obj(chunk))

    return result or [_text_obj("")]


def _split_str(text: str, limit: int = _BLOCK_CHAR_LIMIT) -> list[str]:
    """Split a string into chunks within Notion's per-element size limit."""
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _rich_text(content: str) -> list[dict]:
    """Plain text rich_text (no inline Markdown parsing). Used for metadata fields."""
    return [{"type": "text", "text": {"content": content[:_BLOCK_CHAR_LIMIT]}}]


def _rich_text_md(content: str) -> list[dict]:
    """Rich_text with inline Markdown parsing. Used for note body blocks."""
    return _parse_inline(content)


def _sanitize_caption_rich_text(rich: list[dict]) -> list[dict]:
    """
    Sanitize caption rich_text to avoid Notion validation failures.

    Notion rejects empty inline equations and empty text fragments in image
    captions. We drop such items only for captions, keeping body parsing intact.
    """
    cleaned: list[dict] = []
    for item in rich:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "equation":
            equation = item.get("equation", {})
            expr = equation.get("expression", "") if isinstance(equation, dict) else ""
            if isinstance(expr, str) and expr.strip():
                cleaned.append(item)
            continue

        if item_type == "text":
            text = item.get("text", {})
            content = text.get("content", "") if isinstance(text, dict) else ""
            if isinstance(content, str) and content.strip():
                cleaned.append(item)
            continue

        cleaned.append(item)

    return cleaned


def _caption_rich_text_md(content: str) -> list[dict]:
    """Caption-only rich_text with validation-safe inline cleanup."""
    return _sanitize_caption_rich_text(_parse_inline(content))


def _fig_caption_text(fig: "FigureInfo") -> str:
    """Build prefixed caption '图n:' or '表n:' matching paper figure/table numbers."""
    prefix = "表" if fig.kind == "table" else "图"
    if fig.number:
        prefix = f"{prefix}{fig.number}: "
    else:
        prefix = f"{prefix}: "
    return f"{prefix}{fig.caption}" if fig.caption else prefix.rstrip(": ")


def _paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text_md(text)},
    }


def _heading1_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_1",
        "heading_1": {"rich_text": _rich_text(text)},
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


def _equation_block(expr: str) -> dict:
    return {
        "object": "block",
        "type": "equation",
        "equation": {"expression": expr.strip()},
    }


def _bullet_block(rich: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich},
    }


def _divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _text_to_blocks(text: str) -> list[dict]:
    """Convert plain text into Notion paragraph blocks."""
    return [_paragraph_block(text)]


_FIGURE_MARKER = re.compile(r"^\[FIGURE[:\s]*(\d+)\]$", re.IGNORECASE)
_TABLE_MARKER = re.compile(r"^\[TABLE[:\s]*(\d+)\]$", re.IGNORECASE)


def _freeform_to_blocks(text: str) -> list[dict]:
    """
    Convert freeform Markdown text into Notion blocks.

    Structural elements:
      #        → heading_1
      ##       → heading_2
      ###      → heading_3
      - / *    → bulleted_list_item
      ---      → divider
      $$...$$  → equation block (single-line or multi-line fence)
      \\[...\\]  → equation block (single-line or multi-line fence)
      blank    → skipped

    Inline Markdown (**bold**, *italic*, `code`, [text](url), $...$) is
    parsed within each block via _parse_inline().
    """
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # ── Block equation: $$ ... $$ ────────────────────────────────
        if (
            stripped == "$$"
            or stripped.startswith("$$")
            and stripped.endswith("$$")
            and len(stripped) > 4
        ):
            if (
                stripped.startswith("$$")
                and stripped.endswith("$$")
                and len(stripped) > 4
            ):
                # Single-line: $$expr$$
                expr = stripped[2:-2]
                blocks.append(_equation_block(expr))
                i += 1
                continue
            # Opening $$ fence — collect until closing $$
            expr_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "$$":
                expr_lines.append(lines[i])
                i += 1
            i += 1  # skip closing $$
            blocks.append(_equation_block("\n".join(expr_lines)))
            continue

        # ── Block equation: \[ ... \] ────────────────────────────────
        if stripped == r"\[" or (
            stripped.startswith(r"\[")
            and stripped.endswith(r"\]")
            and len(stripped) > 4
        ):
            if (
                stripped.startswith(r"\[")
                and stripped.endswith(r"\]")
                and len(stripped) > 4
            ):
                expr = stripped[2:-2]
                blocks.append(_equation_block(expr))
                i += 1
                continue
            expr_lines = []
            i += 1
            while i < len(lines) and lines[i].strip() != r"\]":
                expr_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(_equation_block("\n".join(expr_lines)))
            continue

        if not stripped:
            i += 1
            continue

        fig_m = _FIGURE_MARKER.match(stripped)
        if fig_m:
            blocks.append({"_figure_placeholder": int(fig_m.group(1))})
            i += 1
            continue

        tbl_m = _TABLE_MARKER.match(stripped)
        if tbl_m:
            blocks.append({"_table_placeholder": int(tbl_m.group(1))})
            i += 1
            continue

        if stripped == "---":
            blocks.append(_divider_block())
        elif stripped.startswith("#### ") or re.match(r"^#{4,} ", stripped):
            # Demote H4+ to heading_3
            text = re.sub(r"^#{4,} ", "", stripped)
            blocks.append(_heading3_block(text.strip()))
        elif stripped.startswith("### "):
            blocks.append(_heading3_block(stripped[4:].strip()))
        elif stripped.startswith("## "):
            blocks.append(_heading2_block(stripped[3:].strip()))
        elif stripped.startswith("# "):
            blocks.append(_heading1_block(stripped[2:].strip()))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            item = stripped[2:].strip()
            blocks.append(_bullet_block(_rich_text_md(item)))
        else:
            blocks.append(_paragraph_block(stripped))
        i += 1
    return blocks


def _get_heading_text(block: dict) -> "str | None":
    """Extract the plain text content from a heading_1/2/3 block, or None."""
    for htype in ("heading_1", "heading_2", "heading_3"):
        if block.get("type") == htype:
            rich = block.get(htype, {}).get("rich_text", [])
            return "".join(rt.get("text", {}).get("content", "") for rt in rich)
    return None


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
                blocks.append(_bullet_block(_rich_text_md(item)))
        else:
            blocks.extend(_text_to_blocks(content))

    return blocks


class NotionService:
    """Handles all Notion API interactions."""

    def __init__(self) -> None:
        from notion_client import Client

        cfg = get_config()
        self._token = cfg.notion_token
        self._client = Client(auth=self._token)
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

    def _api_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Execute a Notion API call with retry on transient errors."""
        for attempt in range(3):
            try:
                result = fn(*args, **kwargs)
                self._rate_limit()
                return result
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def find_existing_pages(self, paper_url: str) -> list[str]:
        """Return non-archived page IDs whose URL equals ``paper_url``."""
        url_prop = self._props.get("url", "URL")
        page_ids: list[str] = []
        try:
            next_cursor: str | None = None
            while True:
                query_kwargs: dict[str, Any] = {
                    "database_id": self._database_id,
                    "filter": {
                        "property": url_prop,
                        "url": {"equals": paper_url},
                    },
                }
                if next_cursor:
                    query_kwargs["start_cursor"] = next_cursor
                results = self._client.databases.query(**query_kwargs)
                for page in results.get("results", []):
                    if page.get("archived") or page.get("in_trash"):
                        continue
                    page_id = page.get("id")
                    if isinstance(page_id, str):
                        page_ids.append(page_id)
                if not results.get("has_more"):
                    break
                next_cursor = results.get("next_cursor")
        except Exception:
            return []
        return page_ids

    def find_existing_page(self, paper_url: str) -> str | None:
        """Return the first non-archived page ID for this URL, if any."""
        pages = self.find_existing_pages(paper_url)
        return pages[0] if pages else None

    def archive_page(self, page_id: str) -> None:
        """Archive a page so it no longer blocks duplicate checks."""
        self._api_call(
            self._client.pages.update,
            page_id=page_id,
            archived=True,
        )

    def create_page(self, metadata: PaperMetadata) -> str:
        """Create a new database page for the paper. Returns the page ID."""
        props = self._props
        today = datetime.now().date().isoformat()

        properties: dict[str, Any] = {
            props["title"]: {"title": [{"text": {"content": metadata.title}}]},
        }

        if metadata.authors:
            properties[props["authors"]] = {
                "rich_text": _rich_text(metadata.authors_str[:2000])
            }

        properties[props["source"]] = {"select": {"name": metadata.source.value}}

        properties[props["url"]] = {"url": metadata.url}

        if metadata.published_date:
            properties[props["published_date"]] = {
                "date": {"start": metadata.published_date.isoformat()}
            }

        properties[props["added_date"]] = {"date": {"start": today}}

        if self._status_type == "checkbox":
            properties[props["status"]] = {"checkbox": False}
        else:
            properties[props["status"]] = {"select": {"name": self._default_status}}

        response = self._api_call(
            self._client.pages.create,
            parent={"database_id": self._database_id},
            properties=properties,
        )
        return response["id"]

    def update_summary(self, page_id: str, summary: str) -> None:
        """Write LLM-generated one-sentence summary to the abstract property."""
        prop_name = self._props.get("abstract", "")
        if not prop_name or not summary:
            return
        self._api_call(
            self._client.pages.update,
            page_id=page_id,
            properties={prop_name: {"rich_text": _rich_text(summary[:2000])}},
        )

    def update_classifications(
        self, page_id: str, classification: Classification
    ) -> None:
        """Write classification tags to page properties."""
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
            self._api_call(
                self._client.pages.update,
                page_id=page_id,
                properties=prop_updates,
            )

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
            self._api_call(
                self._client.blocks.children.append,
                block_id=page_id,
                children=batch,
            )

    def get_page_url(self, page_id: str) -> str:
        """Return the Notion web URL for the page."""
        clean_id = page_id.replace("-", "")
        return f"https://www.notion.so/{clean_id}"

    # ── Figure upload ─────────────────────────────────────────────────────────

    def _upload_file(self, image_path: Path) -> Optional[str]:
        """
        Upload a single image file to Notion via the file upload API.

        Returns the file_upload_id on success, or None on failure.
        Retries up to 3 times on transient errors.

        Three-step process (per Notion docs):
          1. POST /v1/file_uploads   → get id + upload_url
          2. POST /v1/file_uploads/{id}/send  → upload multipart binary
          3. Return the id for use in image blocks
        """
        content_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        headers_json = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Notion-Version": _NOTION_VERSION,
        }
        headers_upload = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _NOTION_VERSION,
        }

        for attempt in range(3):
            try:
                with httpx.Client(timeout=60) as http:
                    r1 = http.post(
                        f"{_NOTION_API}/file_uploads",
                        headers=headers_json,
                        json={
                            "filename": image_path.name,
                            "content_type": content_type,
                        },
                    )
                    r1.raise_for_status()
                    file_upload_id = r1.json()["id"]

                    with open(image_path, "rb") as fh:
                        r2 = http.post(
                            f"{_NOTION_API}/file_uploads/{file_upload_id}/send",
                            headers=headers_upload,
                            files={"file": (image_path.name, fh, content_type)},
                        )
                    r2.raise_for_status()

                return file_upload_id

            except Exception:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                return None
        return None

    def _figure_blocks(self, fig: FigureInfo, upload_id: str) -> list[dict]:
        """Build image blocks for one figure or table."""
        return [
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "file_upload",
                    "file_upload": {"id": upload_id},
                    "caption": _caption_rich_text_md(_fig_caption_text(fig)),
                },
            },
        ]

    def append_note_with_figures(
        self,
        page_id: str,
        note: PaperNote,
        figures: list[FigureInfo],
        tables: list[FigureInfo] | None = None,
    ) -> int:
        """
        Append note blocks to the page, with figures/tables injected at
        [FIGURE:N] / [TABLE:N] markers placed by the LLM.

        Items not referenced by any marker are appended at the end under
        "论文核心图表" / "论文核心表格" sections.

        Returns the total number of successfully uploaded items.
        """
        tables = tables or []

        if note.is_freeform:
            content_blocks = _freeform_to_blocks(note.raw_content or "")
        else:
            content_blocks = _note_to_blocks(note)

        # Upload all figures
        fig_map: dict[int, FigureInfo] = {fig.number: fig for fig in figures}
        fig_uploads: dict[int, str] = {}
        for fig in figures:
            uid = self._upload_file(fig.image_path)
            if uid is not None:
                fig_uploads[fig.number] = uid
            self._rate_limit()

        # Upload all tables
        tbl_map: dict[int, FigureInfo] = {tbl.number: tbl for tbl in tables}
        tbl_uploads: dict[int, str] = {}
        for tbl in tables:
            uid = self._upload_file(tbl.image_path)
            if uid is not None:
                tbl_uploads[tbl.number] = uid
            self._rate_limit()

        placed_figs: set[int] = set()
        placed_tbls: set[int] = set()
        final_blocks: list[dict] = [_divider_block(), _heading2_block("AI 分析笔记")]

        for block in content_blocks:
            fig_num = block.get("_figure_placeholder")
            tbl_num = block.get("_table_placeholder")
            if fig_num is not None:
                if fig_num in fig_uploads:
                    final_blocks.extend(
                        self._figure_blocks(fig_map[fig_num], fig_uploads[fig_num])
                    )
                    placed_figs.add(fig_num)
            elif tbl_num is not None:
                if tbl_num in tbl_uploads:
                    final_blocks.extend(
                        self._figure_blocks(tbl_map[tbl_num], tbl_uploads[tbl_num])
                    )
                    placed_tbls.add(tbl_num)
            else:
                final_blocks.append(block)

        unplaced_figs = [
            fig
            for fig in figures
            if fig.number not in placed_figs and fig.number in fig_uploads
        ]
        if unplaced_figs:
            final_blocks += [_divider_block(), _heading2_block("论文核心图表")]
            for fig in unplaced_figs:
                final_blocks.extend(self._figure_blocks(fig, fig_uploads[fig.number]))

        unplaced_tbls = [
            tbl
            for tbl in tables
            if tbl.number not in placed_tbls and tbl.number in tbl_uploads
        ]
        if unplaced_tbls:
            final_blocks += [_divider_block(), _heading2_block("论文核心表格")]
            for tbl in unplaced_tbls:
                final_blocks.extend(self._figure_blocks(tbl, tbl_uploads[tbl.number]))

        batch_size = 100
        for i in range(0, len(final_blocks), batch_size):
            self._api_call(
                self._client.blocks.children.append,
                block_id=page_id,
                children=final_blocks[i : i + batch_size],
            )

        return len(fig_uploads) + len(tbl_uploads)

    def append_figures(self, page_id: str, figures: list[FigureInfo]) -> int:
        """
        Upload figures and append them to the Notion page as image blocks.

        Keeps the section heading, then inserts each figure as an image block
        with its caption attached on the image block itself.

        Returns the number of figures successfully uploaded.
        """
        if not figures:
            return 0

        blocks: list[dict] = [
            _divider_block(),
            _heading2_block("论文核心图表"),
        ]

        uploaded = 0
        for fig in figures:
            file_upload_id = self._upload_file(fig.image_path)
            if file_upload_id is None:
                continue

            blocks.append(
                {
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": file_upload_id},
                        "caption": _caption_rich_text_md(_fig_caption_text(fig)),
                    },
                }
            )

            uploaded += 1
            self._rate_limit()

        batch_size = 100
        for i in range(0, len(blocks), batch_size):
            self._api_call(
                self._client.blocks.children.append,
                block_id=page_id,
                children=blocks[i : i + batch_size],
            )

        return uploaded

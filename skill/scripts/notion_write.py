# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27.0",
#     "pyyaml>=6.0",
#     "python-dotenv>=1.0.0",
#     "Pillow>=10.0.0",
# ]
# ///
"""Create a Notion page from paper artifacts and append note content with images."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import httpx
from _backend_config import BackendConfigError, load_notion_config
from _lib import load_config, output_error, output_ok

_NOTION_VERSION = "2022-06-28"
_NOTION_FILE_VERSION = "2026-03-11"
_NOTION_API = "https://api.notion.com/v1"
_BLOCK_CHAR_LIMIT = 1900

_INLINE_MD = re.compile(
    r"\[([^\]\n]+)\]\(([^)\n]+)\)"
    r"|\*\*\*([^*\n]+)\*\*\*"
    r"|\*\*([^*\n]+)\*\*"
    r"|\*([^*\n]+)\*"
    r"|`([^`\n]+)`"
    r"|\\\((.+?)\\\)"
    r"|\$([^$\n]+)\$"
)

_FIGURE_MARKER = re.compile(r"^\[FIGURE[:\s]*(\d+)\]$", re.IGNORECASE)
_TABLE_MARKER = re.compile(r"^\[TABLE[:\s]*(\d+)\]$", re.IGNORECASE)


# ── Notion schema loading ────────────────────────────────────────────────────


def _load_notion_config(interactive: bool = True) -> dict[str, Any]:
    """Load Notion token, database_id, and property mapping."""
    return load_notion_config(interactive=interactive)


# ── Rich text / block builders ───────────────────────────────────────────────


def _split_str(text: str, limit: int = _BLOCK_CHAR_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


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
    return {"type": "equation", "equation": {"expression": expr.strip()}}


def _parse_inline(text: str) -> list[dict]:
    result: list[dict] = []
    pos = 0
    for m in _INLINE_MD.finditer(text):
        before = text[pos : m.start()]
        if before:
            for chunk in _split_str(before):
                result.append(_text_obj(chunk))

        link_label, link_url = m.group(1), m.group(2)
        bold_italic = m.group(3)
        bold = m.group(4)
        italic = m.group(5)
        code = m.group(6)
        math_paren = m.group(7)
        math_dollar = m.group(8)

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

    tail = text[pos:]
    if tail:
        for chunk in _split_str(tail):
            result.append(_text_obj(chunk))
    return result or [_text_obj("")]


def _rich_text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content[:_BLOCK_CHAR_LIMIT]}}]


def _rich_text_md(content: str) -> list[dict]:
    return _parse_inline(content)


def _sanitize_caption_rich_text(rich: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for item in rich:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "equation":
            expr = item.get("equation", {}).get("expression", "")
            if isinstance(expr, str) and expr.strip():
                cleaned.append(item)
        elif item_type == "text":
            content = item.get("text", {}).get("content", "")
            if isinstance(content, str) and content.strip():
                cleaned.append(item)
        else:
            cleaned.append(item)
    return cleaned


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


def _freeform_to_blocks(text: str) -> list[dict]:
    """Convert freeform Markdown to Notion blocks with figure/table placeholders."""
    blocks: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Block equation: $$ ... $$
        if stripped == "$$" or (
            stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4
        ):
            if (
                stripped.startswith("$$")
                and stripped.endswith("$$")
                and len(stripped) > 4
            ):
                blocks.append(_equation_block(stripped[2:-2]))
                i += 1
                continue
            expr_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "$$":
                expr_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(_equation_block("\n".join(expr_lines)))
            continue

        # Block equation: \[ ... \]
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
                blocks.append(_equation_block(stripped[2:-2]))
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
            blocks.append(_heading3_block(re.sub(r"^#{4,} ", "", stripped).strip()))
        elif stripped.startswith("### "):
            blocks.append(_heading3_block(stripped[4:].strip()))
        elif stripped.startswith("## "):
            blocks.append(_heading2_block(stripped[3:].strip()))
        elif stripped.startswith("# "):
            blocks.append(_heading1_block(stripped[2:].strip()))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(_bullet_block(_rich_text_md(stripped[2:].strip())))
        else:
            blocks.append(_paragraph_block(stripped))
        i += 1
    return blocks


def _json_note_to_blocks(note_data: dict) -> list[dict]:
    """Convert structured JSON note to Notion blocks."""
    blocks: list[dict] = []
    sections = [
        ("论文概述", note_data.get("overview", "")),
        ("研究问题", note_data.get("research_problem", "")),
        ("方法论", note_data.get("methodology", "")),
        ("主要贡献", note_data.get("contributions", [])),
        ("实验与结果", note_data.get("experiments", "")),
        ("局限性与未来工作", note_data.get("limitations", "")),
        ("关键要点", note_data.get("key_takeaways", [])),
    ]
    for heading, content in sections:
        blocks.append(_heading2_block(heading))
        if isinstance(content, list):
            for item in content:
                blocks.append(_bullet_block(_rich_text_md(str(item))))
        elif content:
            blocks.append(_paragraph_block(str(content)))
    return blocks


# ── Notion API helpers ───────────────────────────────────────────────────────


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": _NOTION_VERSION,
    }


def _rate_limit():
    time.sleep(0.35)


def _api_call(
    client: httpx.Client, method: str, url: str, headers: dict, **kwargs
) -> dict:
    """HTTP call with retry on transient errors."""
    for attempt in range(3):
        try:
            resp = getattr(client, method)(url, headers=headers, **kwargs)
            resp.raise_for_status()
            _rate_limit()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 or e.response.status_code >= 500:
                time.sleep(2**attempt)
                continue
            raise
        except httpx.TransportError:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError("API call failed after 3 retries")


# ── Image upload ─────────────────────────────────────────────────────────────


def _prepare_image(image_path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Flatten transparent images onto white background for Notion."""
    try:
        from PIL import Image

        with Image.open(str(image_path)) as img:
            has_alpha = "A" in img.getbands() or "transparency" in img.info
            if not has_alpha:
                return image_path, None
            tmpdir = tempfile.TemporaryDirectory()
            output_path = Path(tmpdir.name) / f"{image_path.stem}.png"
            composited = Image.alpha_composite(
                Image.new("RGBA", img.size, (255, 255, 255, 255)),
                img.convert("RGBA"),
            ).convert("RGB")
            composited.save(output_path, format="PNG")
            return output_path, tmpdir
    except Exception:
        return image_path, None


def _upload_file(client: httpx.Client, token: str, image_path: Path) -> str | None:
    """Upload image to Notion file API. Returns file_upload_id or None."""
    upload_path, temp_dir = _prepare_image(image_path)
    content_type = mimetypes.guess_type(str(upload_path))[0] or "image/png"

    headers_json = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": _NOTION_FILE_VERSION,
    }
    headers_upload = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_FILE_VERSION,
    }

    try:
        for attempt in range(3):
            try:
                r1 = client.post(
                    f"{_NOTION_API}/file_uploads",
                    headers=headers_json,
                    json={"filename": upload_path.name, "content_type": content_type},
                )
                r1.raise_for_status()
                file_upload_id = r1.json()["id"]

                with open(upload_path, "rb") as fh:
                    r2 = client.post(
                        f"{_NOTION_API}/file_uploads/{file_upload_id}/send",
                        headers=headers_upload,
                        files={"file": (upload_path.name, fh, content_type)},
                    )
                r2.raise_for_status()
                _rate_limit()
                return file_upload_id
            except Exception:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                return None
        return None
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


# ── Multi-select helpers ─────────────────────────────────────────────────────


def _sanitize_multi_select(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = cleaned.replace(",", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _build_multi_select(values: list[str]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        cleaned = _sanitize_multi_select(str(raw))
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        payload.append({"name": cleaned})
    return payload


# ── Main logic ───────────────────────────────────────────────────────────────


def _create_page(
    client: httpx.Client,
    token: str,
    database_id: str,
    props_map: dict[str, str],
    metadata: dict,
    status_type: str,
    default_status: str,
) -> str:
    """Create Notion page with metadata properties. Returns page_id."""
    headers = _api_headers(token)
    today = datetime.now().date().isoformat()

    properties: dict[str, Any] = {
        props_map.get("title", "论文笔记"): {
            "title": [{"text": {"content": metadata["title"]}}]
        },
    }

    authors = metadata.get("authors", [])
    if authors:
        authors_str = ", ".join(authors)[:2000]
        properties[props_map.get("authors", "作者")] = {
            "rich_text": _rich_text(authors_str)
        }

    source = metadata.get("source", "")
    if source:
        properties[props_map.get("source", "来源")] = {"select": {"name": source}}

    url = metadata.get("url", "")
    if url:
        properties[props_map.get("url", "论文链接")] = {"url": url}

    pub_date = metadata.get("published_date", "")
    if pub_date:
        properties[props_map.get("published_date", "发表日期")] = {
            "date": {"start": pub_date}
        }

    properties[props_map.get("added_date", "添加日期")] = {"date": {"start": today}}

    status_prop = props_map.get("status", "阅读状态")
    if status_type == "checkbox":
        properties[status_prop] = {"checkbox": False}
    else:
        properties[status_prop] = {"select": {"name": default_status}}

    data = _api_call(
        client,
        "post",
        f"{_NOTION_API}/pages",
        headers,
        json={"parent": {"database_id": database_id}, "properties": properties},
    )
    return data["id"]


def _update_summary(
    client: httpx.Client, token: str, page_id: str, props_map: dict, summary: str
):
    prop_name = props_map.get("abstract", "一句话摘要")
    if not prop_name or not summary:
        return
    _api_call(
        client,
        "patch",
        f"{_NOTION_API}/pages/{page_id}",
        _api_headers(token),
        json={"properties": {prop_name: {"rich_text": _rich_text(summary[:2000])}}},
    )


def _update_classifications(
    client: httpx.Client,
    token: str,
    page_id: str,
    props_map: dict,
    classification: dict,
):
    headers = _api_headers(token)
    prop_updates: dict[str, Any] = {}

    research_areas = classification.get("research_areas", [])
    if research_areas:
        tag_prop = props_map.get("tags", "研究领域")
        payload = _build_multi_select(research_areas)
        if payload:
            prop_updates[tag_prop] = {"multi_select": payload}

    paper_type = classification.get("paper_type", [])
    if paper_type:
        type_prop = props_map.get("paper_type", "论文类型")
        payload = _build_multi_select(paper_type)
        if payload:
            prop_updates[type_prop] = {"multi_select": payload}

    institutions = classification.get("institutions", [])
    if institutions:
        inst_prop = props_map.get("institution", "来源机构")
        payload = _build_multi_select(institutions)
        if payload:
            prop_updates[inst_prop] = {"multi_select": payload}

    if prop_updates:
        _api_call(
            client,
            "patch",
            f"{_NOTION_API}/pages/{page_id}",
            headers,
            json={"properties": prop_updates},
        )


def _append_blocks(client: httpx.Client, token: str, page_id: str, blocks: list[dict]):
    """Append blocks to page in batches of 100."""
    headers = _api_headers(token)
    for i in range(0, len(blocks), 100):
        batch = blocks[i : i + 100]
        _api_call(
            client,
            "patch",
            f"{_NOTION_API}/blocks/{page_id}/children",
            headers,
            json={"children": batch},
        )


def _archive_page(client: httpx.Client, token: str, page_id: str):
    _api_call(
        client,
        "patch",
        f"{_NOTION_API}/pages/{page_id}",
        _api_headers(token),
        json={"archived": True},
    )


def _find_existing_pages(
    client: httpx.Client, token: str, database_id: str, url_prop: str, paper_url: str
) -> list[str]:
    headers = _api_headers(token)
    page_ids = []
    next_cursor = None
    while True:
        body: dict = {"filter": {"property": url_prop, "url": {"equals": paper_url}}}
        if next_cursor:
            body["start_cursor"] = next_cursor
        data = _api_call(
            client,
            "post",
            f"{_NOTION_API}/databases/{database_id}/query",
            headers,
            json=body,
        )
        for page in data.get("results", []):
            if page.get("archived") or page.get("in_trash"):
                continue
            page_ids.append(page["id"])
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")
    return page_ids


def main():
    parser = argparse.ArgumentParser(description="Write paper artifacts to Notion")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument(
        "--force", action="store_true", help="Archive existing page and re-create"
    )
    parser.add_argument("--skip-images", action="store_true", help="Skip image uploads")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt for missing backend config; emit a structured error",
    )
    args = parser.parse_args()

    paper_dir = Path(args.paper_dir)
    metadata_path = paper_dir / "metadata.json"
    if not metadata_path.exists():
        output_error("metadata.json not found in paper directory")
        return

    metadata = json.loads(metadata_path.read_text())
    try:
        notion_cfg = _load_notion_config(interactive=not args.non_interactive)
    except BackendConfigError as e:
        output_error(
            f"Notion backend needs configuration: {e}",
            backend=e.backend,
            missing=e.missing,
            hint="Run interactively or fill backends/notion/backend.yaml",
        )
        sys.exit(1)
    token = notion_cfg["token"]
    database_id = notion_cfg["database_id"]
    props_map = notion_cfg["properties"]

    with httpx.Client(timeout=60) as client:
        # Check for existing page
        paper_url = metadata.get("url", "")
        url_prop = props_map.get("url", "论文链接")
        if paper_url:
            existing = _find_existing_pages(
                client, token, database_id, url_prop, paper_url
            )
            if existing and not args.force:
                page_url = f"https://www.notion.so/{existing[0].replace('-', '')}"
                output_ok(
                    "Page already exists (use --force to re-create)",
                    exists=True,
                    page_id=existing[0],
                    page_url=page_url,
                )
                return
            if existing and args.force:
                for pid in existing:
                    _archive_page(client, token, pid)

        # Create page
        page_id = _create_page(
            client,
            token,
            database_id,
            props_map,
            metadata,
            notion_cfg["status_type"],
            notion_cfg["default_status"],
        )

        # Update summary
        summary_path = paper_dir / "summary.txt"
        if summary_path.exists():
            _update_summary(
                client, token, page_id, props_map, summary_path.read_text().strip()
            )

        # Update classifications
        classification_path = paper_dir / "classification.json"
        if classification_path.exists():
            classification = json.loads(classification_path.read_text())
            _update_classifications(client, token, page_id, props_map, classification)

        # Determine note format and build content blocks
        config = load_config()
        note_format = config.get("llm", {}).get("note_format", "json")

        notes_md_path = paper_dir / "notes.md"
        notes_json_path = paper_dir / "notes.json"

        content_blocks: list[dict] = []
        if notes_md_path.exists():
            note_format = "freeform"
            content_blocks = _freeform_to_blocks(notes_md_path.read_text())
        elif notes_json_path.exists():
            note_format = "json"
            note_data = json.loads(notes_json_path.read_text())
            content_blocks = _json_note_to_blocks(note_data)

        # Upload images and resolve placeholders
        uploaded_count = 0
        if not args.skip_images:
            visuals_path = paper_dir / "visuals.json"
            if visuals_path.exists():
                visuals = json.loads(visuals_path.read_text())
                fig_uploads: dict[int, str] = {}
                tbl_uploads: dict[int, str] = {}
                fig_captions: dict[int, str] = {}
                tbl_captions: dict[int, str] = {}

                # Load translated captions if available
                captions_path = paper_dir / "captions.json"
                translated: dict[int, str] = {}
                if captions_path.exists():
                    for cap in json.loads(captions_path.read_text()):
                        translated[cap["index"]] = cap.get(
                            "translated", cap.get("original", "")
                        )

                for idx, v in enumerate(visuals):
                    img_path = Path(v["image_path"])
                    if not img_path.is_absolute():
                        img_path = paper_dir / img_path
                    if not img_path.exists():
                        continue

                    uid = _upload_file(client, token, img_path)
                    if uid is None:
                        continue

                    kind = v.get("kind", "figure")
                    number = v.get("number", 0)
                    caption = translated.get(idx, v.get("caption", ""))

                    prefix = "表" if kind == "table" else "图"
                    full_caption = (
                        f"{prefix}{number}: {caption}"
                        if caption
                        else f"{prefix}{number}"
                    )

                    if kind == "table":
                        tbl_uploads[number] = uid
                        tbl_captions[number] = full_caption
                    else:
                        fig_uploads[number] = uid
                        fig_captions[number] = full_caption

                    uploaded_count += 1

                # Resolve placeholders in content blocks
                placed_figs: set[int] = set()
                placed_tbls: set[int] = set()
                final_blocks: list[dict] = [
                    _divider_block(),
                    _heading2_block("AI 分析笔记"),
                ]

                for block in content_blocks:
                    fig_num = block.get("_figure_placeholder")
                    tbl_num = block.get("_table_placeholder")
                    if fig_num is not None:
                        if fig_num in fig_uploads:
                            caption_rich = _sanitize_caption_rich_text(
                                _rich_text_md(fig_captions[fig_num])
                            )
                            final_blocks.append(
                                {
                                    "object": "block",
                                    "type": "image",
                                    "image": {
                                        "type": "file_upload",
                                        "file_upload": {"id": fig_uploads[fig_num]},
                                        "caption": caption_rich,
                                    },
                                }
                            )
                            placed_figs.add(fig_num)
                    elif tbl_num is not None:
                        if tbl_num in tbl_uploads:
                            caption_rich = _sanitize_caption_rich_text(
                                _rich_text_md(tbl_captions[tbl_num])
                            )
                            final_blocks.append(
                                {
                                    "object": "block",
                                    "type": "image",
                                    "image": {
                                        "type": "file_upload",
                                        "file_upload": {"id": tbl_uploads[tbl_num]},
                                        "caption": caption_rich,
                                    },
                                }
                            )
                            placed_tbls.add(tbl_num)
                    else:
                        final_blocks.append(block)

                # Append unplaced figures
                unplaced_figs = [n for n in fig_uploads if n not in placed_figs]
                if unplaced_figs:
                    final_blocks += [_divider_block(), _heading2_block("论文核心图表")]
                    for n in sorted(unplaced_figs):
                        caption_rich = _sanitize_caption_rich_text(
                            _rich_text_md(fig_captions[n])
                        )
                        final_blocks.append(
                            {
                                "object": "block",
                                "type": "image",
                                "image": {
                                    "type": "file_upload",
                                    "file_upload": {"id": fig_uploads[n]},
                                    "caption": caption_rich,
                                },
                            }
                        )

                # Append unplaced tables
                unplaced_tbls = [n for n in tbl_uploads if n not in placed_tbls]
                if unplaced_tbls:
                    final_blocks += [_divider_block(), _heading2_block("论文核心表格")]
                    for n in sorted(unplaced_tbls):
                        caption_rich = _sanitize_caption_rich_text(
                            _rich_text_md(tbl_captions[n])
                        )
                        final_blocks.append(
                            {
                                "object": "block",
                                "type": "image",
                                "image": {
                                    "type": "file_upload",
                                    "file_upload": {"id": tbl_uploads[n]},
                                    "caption": caption_rich,
                                },
                            }
                        )

                _append_blocks(client, token, page_id, final_blocks)
            else:
                # No visuals, just append note blocks
                if content_blocks:
                    all_blocks = [
                        _divider_block(),
                        _heading2_block("AI 分析笔记"),
                    ] + content_blocks
                    _append_blocks(client, token, page_id, all_blocks)
        else:
            # Skip images: just append note content
            if content_blocks:
                all_blocks = [
                    _divider_block(),
                    _heading2_block("AI 分析笔记"),
                ] + content_blocks
                _append_blocks(client, token, page_id, all_blocks)

    page_url = f"https://www.notion.so/{page_id.replace('-', '')}"
    output_ok(
        f"Created Notion page with {uploaded_count} images",
        page_id=page_id,
        page_url=page_url,
        images_uploaded=uploaded_count,
        note_format=note_format,
    )


if __name__ == "__main__":
    main()

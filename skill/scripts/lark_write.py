# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0", "python-dotenv>=1.0.0", "Pillow>=10.0.0"]
# ///
"""Create a Lark (飞书) document from paper artifacts with embedded figures and tables.

Mirrors notion_write.py but outputs to Lark docx documents instead of Notion.
Uses lark-cli as a subprocess for all Lark API operations.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).parent))
from _backend_config import BackendConfigError, load_lark_config
from _lib import find_project_root, output_error, output_ok

_FIGURE_MARKER = re.compile(r"^\[FIGURE[:\s]*(\d+)\]$", re.IGNORECASE)
_TABLE_MARKER = re.compile(r"^\[TABLE[:\s]*(\d+)\]$", re.IGNORECASE)


# ── Lark CLI helpers ────────────────────────────────────────────────────────


def _lark(*args: str, stdin_content: str | None = None) -> dict:
    """Run lark-cli with given args, return parsed JSON output."""
    cmd = ["lark-cli", *args, "--json"]
    try:
        result = subprocess.run(
            cmd,
            input=stdin_content,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = result.stdout.strip()
        if not out:
            raise RuntimeError(
                f"lark-cli returned empty stdout (stderr: {result.stderr.strip()})"
            )
        data = json.loads(out)
        if not data.get("ok", False):
            err = data.get("error", {}).get("message", out)
            raise RuntimeError(f"lark-cli error: {err}")
        return data
    except json.JSONDecodeError:
        raise RuntimeError(
            f"lark-cli returned non-JSON output: {result.stdout[:500]}"
            f"\nstderr: {result.stderr[:500]}"
        )


def _load_lark_config(interactive: bool = True) -> dict:
    """Load Lark configuration from backends/lark/backend.yaml."""
    cfg = load_lark_config(interactive=interactive)
    return {
        "identity": cfg["identity"],
        "parent_token": cfg["parent_token"],
    }


def _create_doc(xml_content: str, lark_cfg: dict) -> dict:
    """Create a Lark docx document and return parsed response."""
    identity = lark_cfg["identity"]
    parent_token = lark_cfg["parent_token"]

    args = [
        "docs",
        "+create",
        "--api-version",
        "v2",
        "--as",
        identity,
        "--content",
        xml_content,
    ]
    if parent_token:
        args.extend(["--parent-token", parent_token])

    return _lark(*args)


def _relative_to_cwd(path: Path) -> Path:
    """Return a path relative to cwd; fall back to original if not possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path


def _upload_image_to_drive(image_path: Path, lark_cfg: dict) -> str:
    """Upload an image to Drive and return the file_token for docx embedding.

    The returned token can be used in docx XML as ``<img src="<token>" />``.
    """
    identity = lark_cfg["identity"]
    rel_path = _relative_to_cwd(image_path)
    result = _lark(
        "drive",
        "+upload",
        "--file",
        str(rel_path),
        "--as",
        identity,
    )
    token = result.get("data", {}).get("file_token", "")
    if not token:
        raise RuntimeError(f"drive +upload returned no file_token for {image_path}")
    return token


# ── XML builders ─────────────────────────────────────────────────────────────


def _escape(text: str) -> str:
    """XML-escape text content (not tags)."""
    return xml_escape(text, entities={'"': "&quot;", "'": "&apos;"})


def _text_with_url(label: str, url: str) -> str:
    """Return a link tag: <a href="url">label</a>."""
    return f'<a href="{_escape(url)}">{_escape(label)}</a>'


def _h1(text: str) -> str:
    return f"<h1>{_escape(text)}</h1>"


def _h2(text: str) -> str:
    return f"<h2>{_escape(text)}</h2>"


def _h3(text: str) -> str:
    return f"<h3>{_escape(text)}</h3>"


def _p(text: str) -> str:
    return f"<p>{_escape(text)}</p>"


def _rich_p(text: str) -> str:
    """Paragraph with basic Markdown-like inline formatting mapped to XML."""
    # Bold: **text** → <b>text</b>
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", text)
    # Italic: *text* → <em>text</em>
    text = re.sub(r"\*([^*\n]+?)\*", r"<em>\1</em>", text)
    # Inline code: `text` → <code>text</code>
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    # Links: [text](url) → <a href="url">text</a>
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{_escape(m.group(2))}">{_escape(m.group(1))}</a>',
        text,
    )
    # LaTeX inline: $...$ → <latex>...</latex>
    text = re.sub(r"\$([^$\n]+?)\$", r"<latex>\1</latex>", text)
    # Escape remaining XML special chars in text (but not our tags)
    # We do this after inserting tags, so we need a different approach.
    # Strategy: use placeholder markers, escape, then restore.
    return f"<p>{text}</p>"


def _callout(emoji: str, bg: str, border: str, text_color: str, content: str) -> str:
    return (
        f'<callout emoji="{_escape(emoji)}"'
        f' background-color="{_escape(bg)}"'
        f' border-color="{_escape(border)}"'
        f' text-color="{_escape(text_color)}">'
        f"{content}"
        f"</callout>"
    )


def _ul(items: list[str]) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in items) + "</ul>"


def _hr() -> str:
    return "<hr/>"


# ── Content builders ─────────────────────────────────────────────────────────


def _build_metadata_xml(metadata: dict) -> str:
    """Build metadata section XML (info table)."""
    title = metadata.get("title", "Untitled")
    authors = ", ".join(metadata.get("authors", [])) or "—"
    source = metadata.get("source", "—")
    url = metadata.get("url", "")
    pub_date = metadata.get("published_date", "—")
    today = datetime.now().date().isoformat()

    rows = [
        ("作者", authors),
        ("来源", source),
        ("发表日期", pub_date),
        ("论文链接", _text_with_url(url, url) if url else "—"),
        ("添加日期", today),
    ]

    table_rows = "\n".join(
        f'<tr><td background-color="light-gray"><b>{_escape(k)}</b></td><td>{v}</td></tr>'
        for k, v in rows
    )

    return (
        f"<title>{_escape(title)}</title>\n"
        f"<h1>{_escape(title)}</h1>\n"
        f"<h2>📄 论文信息</h2>\n"
        f"<table>\n"
        f"<tbody>\n{table_rows}\n</tbody>\n"
        f"</table>"
    )


def _build_summary_xml(summary: str) -> str:
    """Build summary callout block."""
    if not summary:
        return ""
    return _callout(
        "💡",
        "light-blue",
        "blue",
        "blue",
        f"<h3>一句话摘要</h3><p>{_escape(summary)}</p>",
    )


def _build_classification_xml(classification: dict) -> str:
    """Build classification section XML."""
    parts = []

    research_areas = classification.get("research_areas", [])
    if research_areas:
        parts.append(f"<p><b>研究领域：</b>{_escape('、'.join(research_areas))}</p>")

    paper_type = classification.get("paper_type", [])
    if paper_type:
        parts.append(f"<p><b>论文类型：</b>{_escape('、'.join(paper_type))}</p>")

    institutions = classification.get("institutions", [])
    if institutions:
        parts.append(f"<p><b>来源机构：</b>{_escape('、'.join(institutions))}</p>")

    if not parts:
        return ""

    return "<h2>🏷️ 论文分类</h2>\n" + "\n".join(parts)


def _freeform_to_xml(text: str) -> str:
    """Convert freeform Markdown notes to Lark XML blocks."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Convert figure/table markers into placeholders that main() will
        # replace with <img src="drive_file_token" /> after uploading.
        fig_m = _FIGURE_MARKER.match(stripped)
        tbl_m = _TABLE_MARKER.match(stripped)
        if fig_m:
            blocks.append(
                f'<p data-lark-media="figure" data-lark-index="{fig_m.group(1)}">'
                f"{_escape(stripped)}</p>"
            )
            i += 1
            continue
        if tbl_m:
            blocks.append(
                f'<p data-lark-media="table" data-lark-index="{tbl_m.group(1)}">'
                f"{_escape(stripped)}</p>"
            )
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            blocks.append(_hr())
        elif stripped.startswith("#### ") or re.match(r"^#{4,} ", stripped):
            blocks.append(_h3(re.sub(r"^#{4,} ", "", stripped).strip()))
        elif stripped.startswith("### "):
            blocks.append(_h3(stripped[4:].strip()))
        elif stripped.startswith("## "):
            blocks.append(_h2(stripped[3:].strip()))
        elif stripped.startswith("# "):
            blocks.append(_h1(stripped[2:].strip()))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(_rich_p(stripped[2:].strip()))
        else:
            blocks.append(_rich_p(stripped))
        i += 1
    return "\n".join(blocks)


def _json_note_to_xml(note_data: dict) -> str:
    """Convert structured JSON notes to Lark XML blocks."""
    blocks: list[str] = []
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
        if isinstance(content, list):
            if not content:
                continue
            blocks.append(_h2(heading))
            items = "".join(f"<li>{_escape(str(item))}</li>" for item in content)
            blocks.append(f"<ul>{items}</ul>")
        elif content:
            blocks.append(_h2(heading))
            blocks.append(_rich_p(str(content)))
    return "\n".join(blocks)


def _build_notes_xml(paper_dir: Path) -> tuple[str, str]:
    """Build notes XML from notes.md or notes.json. Returns (xml, format_name)."""
    notes_md = paper_dir / "notes.md"
    notes_json = paper_dir / "notes.json"

    if notes_md.exists():
        content = notes_md.read_text(encoding="utf-8").strip()
        return _freeform_to_xml(content), "freeform"
    elif notes_json.exists():
        note_data = json.loads(notes_json.read_text(encoding="utf-8"))
        return _json_note_to_xml(note_data), "json"
    return "", "none"


def _prepare_image(image_path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Flatten transparent images onto white background (for upload compatibility)."""
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


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Write paper artifacts to Lark doc")
    parser.add_argument("paper_dir", help="Path to paper directory")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip dedup check and always create new doc",
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

    project_root = find_project_root()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    try:
        lark_cfg = _load_lark_config(interactive=not args.non_interactive)
    except BackendConfigError as e:
        output_error(
            f"Lark backend needs configuration: {e}",
            backend=e.backend,
            missing=e.missing,
            hint="Run interactively or fill backends/lark/backend.yaml",
        )
        sys.exit(1)

    # ── Build document content ───────────────────────────────────────────
    xml_parts: list[str] = []

    # 1. Metadata header
    xml_parts.append(_build_metadata_xml(metadata))

    # 2. Summary
    summary_path = paper_dir / "summary.txt"
    if summary_path.exists():
        summary = summary_path.read_text(encoding="utf-8").strip()
        summary_xml = _build_summary_xml(summary)
        if summary_xml:
            xml_parts.append(summary_xml)

    # 3. Classification
    classification_path = paper_dir / "classification.json"
    if classification_path.exists():
        classification = json.loads(classification_path.read_text(encoding="utf-8"))
        class_xml = _build_classification_xml(classification)
        if class_xml:
            xml_parts.append(class_xml)

    # 4. AI Notes
    notes_xml, note_format = _build_notes_xml(paper_dir)
    if notes_xml:
        xml_parts.append(_hr())
        xml_parts.append(_h2("📝 AI 分析笔记"))
        xml_parts.append(notes_xml)

    doc_xml = "\n".join(xml_parts)

    # ── Upload images and embed them into the XML ────────────────────────
    uploaded_count = 0
    if not args.skip_images:
        visuals_path = paper_dir / "visuals.json"
        if visuals_path.exists():
            visuals = json.loads(visuals_path.read_text(encoding="utf-8"))

            # Load translated captions if available
            captions_path = paper_dir / "captions.json"
            translated: dict[int, str] = {}
            if captions_path.exists():
                for cap in json.loads(captions_path.read_text(encoding="utf-8")):
                    translated[cap["index"]] = cap.get(
                        "translated", cap.get("original", "")
                    )

            # Build a lookup keyed by (kind, number) so placeholders can be resolved.
            visual_by_key: dict[tuple[str, int], dict] = {}
            for v in visuals:
                key = (v.get("kind", "figure"), v.get("number", 0))
                visual_by_key[key] = v

            # Only upload visuals actually referenced by placeholders in the doc.
            referenced_keys: set[tuple[str, int]] = set()
            for kind, number in re.findall(
                r'<p data-lark-media="(figure|table)" data-lark-index="(\d+)">',
                doc_xml,
            ):
                referenced_keys.add((kind, int(number)))

            # Upload each referenced image to Drive and collect file_tokens.
            token_by_key: dict[tuple[str, int], str] = {}
            tempdirs: list[tempfile.TemporaryDirectory] = []
            for key in sorted(referenced_keys):
                v = visual_by_key.get(key)
                if v is None:
                    continue
                img_path = Path(v["image_path"])
                if not img_path.is_absolute():
                    # visuals.json stores paths relative to the project root.
                    img_path = project_root / img_path
                if not img_path.exists():
                    continue

                upload_path, tmpdir = _prepare_image(img_path)
                if tmpdir is not None:
                    tempdirs.append(tmpdir)
                try:
                    token = _upload_image_to_drive(upload_path, lark_cfg)
                    token_by_key[key] = token
                    uploaded_count += 1
                except RuntimeError as e:
                    print(
                        f"  [warn] Failed to upload {img_path.name}: {e}",
                        file=sys.stderr,
                    )

            # Replace placeholders like <p data-lark-media="figure" data-lark-index="1">...</p>
            # with the uploaded image (plus a caption paragraph).
            def _replace_media_placeholder(m: re.Match) -> str:
                kind = m.group(1)
                number = int(m.group(2))
                v = visual_by_key.get((kind, number))
                if v is None:
                    return ""
                token = token_by_key.get((kind, number))
                if not token:
                    return ""

                # Find the visual's list index for caption lookup.
                caption = ""
                for idx, item in enumerate(visuals):
                    if (
                        item.get("kind", "figure") == kind
                        and item.get("number", 0) == number
                    ):
                        caption = translated.get(idx, item.get("caption", ""))
                        break

                prefix = "表" if kind == "table" else "图"
                parts: list[str] = []
                if caption:
                    parts.append(
                        f"<p><b>{_escape(prefix)}{number}:</b> {_escape(caption)}</p>"
                    )
                parts.append(f'<img src="{_escape(token)}" />')
                return "\n".join(parts)

            doc_xml = re.sub(
                r'<p data-lark-media="(figure|table)" data-lark-index="(\d+)">.*?</p>',
                _replace_media_placeholder,
                doc_xml,
                flags=re.DOTALL,
            )

            # Clean up temporary directories used for alpha compositing.
            for tmpdir in tempdirs:
                tmpdir.cleanup()

    # ── Create Lark document ─────────────────────────────────────────────
    try:
        result = _create_doc(doc_xml, lark_cfg)
    except RuntimeError as e:
        output_error(f"Failed to create Lark doc: {e}")
        return

    doc_data = result.get("data", {}).get("document", {})
    doc_id = doc_data.get("document_id", "")
    doc_url = doc_data.get("url", "")

    if not doc_id:
        output_error("Lark doc created but no document_id in response", raw=result)
        return

    output_ok(
        f"Created Lark doc with {uploaded_count} images",
        doc_id=doc_id,
        doc_url=doc_url,
        images_uploaded=uploaded_count,
        note_format=note_format,
    )


if __name__ == "__main__":
    main()

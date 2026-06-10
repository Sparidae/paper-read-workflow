# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27.0",
#     "pyyaml>=6.0",
#     "python-dotenv>=1.0.0",
#     "openreview-py>=1.0.0",
# ]
# ///
"""Download a paper (PDF + LaTeX source + metadata) from a URL."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_config, output_error, output_ok, papers_dir

_ARXIV_ID_RE = re.compile(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?")
_ARXIV_DOMAINS = {"arxiv.org", "alphaxiv.org", "ar5iv.labs.google.com"}
_IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
_MAX_IMG_BYTES = 25 * 1024 * 1024
_HEADERS = {"User-Agent": "paper-tool/0.1"}


@dataclass
class PaperMetadata:
    title: str
    authors: list[str]
    abstract: str
    source: str
    url: str
    paper_id: str
    published_date: str | None = None
    tags: list[str] = field(default_factory=list)


def _detect_source(url: str) -> str:
    url_lower = url.lower()
    if any(d in url_lower for d in _ARXIV_DOMAINS):
        return "arxiv"
    if "huggingface.co/papers/" in url_lower:
        return "arxiv"
    if _ARXIV_ID_RE.fullmatch(url.strip()):
        return "arxiv"
    if "openreview.net" in url_lower:
        return "openreview"
    return "unknown"


def _extract_arxiv_id(url: str) -> str:
    m = _ARXIV_ID_RE.search(url)
    if m:
        return m.group(0)
    raise ValueError(f"Could not extract Arxiv ID from: {url}")


def _make_paper_dir(paper_id: str, title: str, dest_dir: Path) -> Path:
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).replace(" ", "_")[:80]
    paper_dir = dest_dir / f"{paper_id}_{safe_title}"
    paper_dir.mkdir(parents=True, exist_ok=True)
    return paper_dir


# ── Arxiv ───────────────────────────────────────────────────────────────────


def _fetch_arxiv_metadata(url: str) -> PaperMetadata:
    from html.parser import HTMLParser

    paper_id = _extract_arxiv_id(url)
    base_id = re.sub(r"v\d+$", "", paper_id)
    abs_url = f"https://arxiv.org/abs/{base_id}"

    for attempt in range(3):
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as client:
                response = client.get(abs_url, headers=_HEADERS)
                response.raise_for_status()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)

    html = response.text

    class _MetaParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.meta: dict[str, list[str]] = {}

        def handle_starttag(self, tag, attrs):
            if tag == "meta":
                d = dict(attrs)
                name = d.get("name", "")
                if name.startswith("citation_"):
                    self.meta.setdefault(name, []).append(d.get("content", ""))

    parser = _MetaParser()
    parser.feed(html)
    m = parser.meta

    title = (m.get("citation_title") or [""])[0].strip()
    if not title:
        raise ValueError(f"Could not parse title from {abs_url}")

    authors = m.get("citation_author") or []
    abstract = (m.get("citation_abstract") or [""])[0].strip().replace("\n", " ")

    published_date = None
    raw_date = (m.get("citation_date") or [""])[0]
    if raw_date:
        try:
            published_date = date.fromisoformat(raw_date.replace("/", "-")).isoformat()
        except ValueError:
            pass

    subjects_match = re.search(
        r'class="tablecell subjects">(.*?)</td>', html, re.DOTALL
    )
    tags: list[str] = []
    if subjects_match:
        tags = re.findall(r"\(([a-z\-]+\.[A-Z]+)\)", subjects_match.group(1))

    return PaperMetadata(
        title=title,
        authors=authors,
        abstract=abstract,
        source="Arxiv",
        url=f"https://arxiv.org/abs/{base_id}",
        paper_id=base_id,
        published_date=published_date,
        tags=tags,
    )


def _download_arxiv_pdf(metadata: PaperMetadata, paper_dir: Path) -> Path:
    dest_path = paper_dir / "paper.pdf"
    if dest_path.exists():
        return dest_path

    pdf_url = f"https://arxiv.org/pdf/{metadata.paper_id}.pdf"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for attempt in range(3):
            try:
                with client.stream("GET", pdf_url, headers=_HEADERS) as response:
                    response.raise_for_status()
                    with open(dest_path, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            f.write(chunk)
                return dest_path
            except httpx.HTTPStatusError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
    return dest_path


def _safe_member_relpath(name: str) -> Path | None:
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".", "..")]
    if not parts:
        return None
    return Path(*parts)


def _normalize_tex_path(raw_path: str, base_dir: Path) -> Path:
    candidate = Path(raw_path.strip())
    if candidate.suffix != ".tex":
        candidate = candidate.with_suffix(".tex")
    normalized: list[str] = []
    for part in (base_dir / candidate).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            continue
        normalized.append(part)
    return Path(*normalized)


def _pick_root_tex(tex_files: dict[Path, str]) -> Path | None:
    candidates = [
        p
        for p, c in tex_files.items()
        if "\\documentclass" in c and "\\begin{document}" in c
    ]
    if not candidates:
        return None
    preferred = {"main.tex": 0, "paper.tex": 1}
    return min(
        candidates,
        key=lambda p: (preferred.get(p.name.lower(), 2), len(p.parts), p.as_posix()),
    )


def _expand_tex_includes(
    path: Path, tex_files: dict[Path, str], visited: set[Path] | None = None
) -> str:
    if visited is None:
        visited = set()
    if path in visited:
        return ""
    content = tex_files.get(path)
    if content is None:
        return ""
    visited.add(path)
    include_re = re.compile(
        r"^(?P<prefix>[^%]*?)\\(?P<cmd>input|include)\{(?P<target>[^{}]+)\}"
    )
    lines: list[str] = []
    for line in content.splitlines():
        match = include_re.match(line)
        if not match:
            lines.append(line)
            continue
        target_path = _normalize_tex_path(match.group("target"), path.parent)
        nested = tex_files.get(target_path)
        if nested is None:
            root_path = _normalize_tex_path(match.group("target"), Path())
            target_path = root_path
            nested = tex_files.get(root_path)
        if nested is None:
            lines.append(line)
            continue
        prefix = match.group("prefix")
        if prefix.strip():
            lines.append(line)
            continue
        lines.append(f"% --- begin include: {target_path.as_posix()} ---")
        lines.append(_expand_tex_includes(target_path, tex_files, visited))
        lines.append(f"% --- end include: {target_path.as_posix()} ---")
    return "\n".join(lines)


def _download_arxiv_latex(metadata: PaperMetadata, paper_dir: Path) -> Path | None:
    merged_path = paper_dir / "paper.tex"
    figures_dir = paper_dir / "figures"
    source_dir = paper_dir / "source"

    if merged_path.exists() and figures_dir.exists() and source_dir.exists():
        return merged_path

    src_url = f"https://arxiv.org/src/{metadata.paper_id}"
    try:
        with httpx.Client(follow_redirects=True, timeout=60) as client:
            for attempt in range(3):
                try:
                    response = client.get(src_url, headers=_HEADERS)
                    response.raise_for_status()
                    break
                except Exception:
                    if attempt == 2:
                        return None
                    time.sleep(2**attempt)

            content_type = response.headers.get("content-type", "")
            if "pdf" in content_type or len(response.content) < 1000:
                return None

            tar_path = paper_dir / "src.tar.gz"
            tar_path.write_bytes(response.content)

            try:
                with tarfile.open(tar_path) as tar:
                    tex_files: dict[Path, str] = {}
                    tex_contents: list[str] = []
                    figures_dir.mkdir(parents=True, exist_ok=True)
                    source_dir.mkdir(parents=True, exist_ok=True)

                    for member in tar.getmembers():
                        rel_path = _safe_member_relpath(member.name)
                        if rel_path is None or not member.isfile():
                            continue
                        f = tar.extractfile(member)
                        if not f:
                            continue
                        try:
                            content = f.read()
                        except Exception:
                            continue

                        source_out = source_dir / rel_path
                        source_out.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            source_out.write_bytes(content)
                        except Exception:
                            pass

                        suffix = Path(member.name).suffix.lower()
                        if member.name.endswith(".tex"):
                            try:
                                decoded = content.decode("utf-8", errors="replace")
                                tex_contents.append(decoded)
                                tex_files[rel_path] = decoded
                            except Exception:
                                pass
                        elif suffix in _IMG_SUFFIXES and member.size <= _MAX_IMG_BYTES:
                            fname = Path(member.name).name
                            out_path = figures_dir / fname
                            if not out_path.exists():
                                try:
                                    out_path.write_bytes(content)
                                except Exception:
                                    pass

                    if not tex_contents:
                        return None

                    root_tex = _pick_root_tex(tex_files)
                    merged_tex = (
                        _expand_tex_includes(root_tex, tex_files) if root_tex else ""
                    )
                    if not merged_tex.strip():
                        merged_tex = "\n\n% --- next file ---\n\n".join(tex_contents)

                    merged_path.write_text(merged_tex)
            finally:
                tar_path.unlink(missing_ok=True)

        return merged_path
    except Exception:
        return None


# ── OpenReview ──────────────────────────────────────────────────────────────


def _extract_forum_id(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "id" in params:
        return params["id"][0]
    raise ValueError(f"Could not extract OpenReview ID from: {url}")


def _fetch_openreview_metadata(url: str) -> PaperMetadata:
    import openreview

    forum_id = _extract_forum_id(url)

    import os

    kwargs: dict = {"baseurl": "https://api2.openreview.net"}
    username = os.getenv("OPENREVIEW_USERNAME", "")
    password = os.getenv("OPENREVIEW_PASSWORD", "")
    if username and password:
        kwargs["username"] = username
        kwargs["password"] = password

    client = openreview.api.OpenReviewClient(**kwargs)
    note = client.get_note(forum_id)
    content = note.content or {}

    def _str(val) -> str:
        if isinstance(val, dict):
            return val.get("value", "")
        return str(val) if val else ""

    title = _str(content.get("title", ""))
    abstract = _str(content.get("abstract", "")).replace("\n", " ")

    raw_authors = content.get("authors", [])
    if isinstance(raw_authors, dict):
        raw_authors = raw_authors.get("value", [])
    authors = [str(a) for a in raw_authors]

    keywords_raw = content.get("keywords", [])
    if isinstance(keywords_raw, dict):
        keywords_raw = keywords_raw.get("value", [])
    tags = [str(k) for k in keywords_raw]

    published_date = None
    if hasattr(note, "tcdate") and note.tcdate:
        try:
            from datetime import datetime

            published_date = (
                datetime.fromtimestamp(note.tcdate / 1000).date().isoformat()
            )
        except Exception:
            pass

    return PaperMetadata(
        title=title,
        authors=authors,
        abstract=abstract,
        source="OpenReview",
        url=f"https://openreview.net/forum?id={forum_id}",
        paper_id=forum_id,
        published_date=published_date,
        tags=tags,
    )


def _download_openreview_pdf(metadata: PaperMetadata, paper_dir: Path) -> Path:
    dest_path = paper_dir / "paper.pdf"
    if dest_path.exists():
        return dest_path

    pdf_url = f"https://openreview.net/pdf?id={metadata.paper_id}"
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for attempt in range(3):
            try:
                with client.stream("GET", pdf_url, headers=_HEADERS) as response:
                    response.raise_for_status()
                    with open(dest_path, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            f.write(chunk)
                return dest_path
            except httpx.HTTPStatusError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
    return dest_path


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Download paper from URL")
    parser.add_argument(
        "url",
        help="Paper URL (arxiv, OpenReview, HuggingFace papers, or bare arxiv ID)",
    )
    parser.add_argument("--papers-dir", help="Override papers storage directory")
    args = parser.parse_args()

    config = load_config()
    dest_dir = Path(args.papers_dir) if args.papers_dir else papers_dir(config)

    url = args.url.strip()
    source = _detect_source(url)

    if source == "unknown":
        output_error(
            f"Unsupported URL: {url}",
            supported="arxiv.org, openreview.net, huggingface.co/papers, bare arxiv ID",
        )
        return

    try:
        if source == "arxiv":
            metadata = _fetch_arxiv_metadata(url)
        else:
            metadata = _fetch_openreview_metadata(url)
    except Exception as e:
        output_error(f"Failed to fetch metadata: {e}")
        return

    paper_dir = _make_paper_dir(metadata.paper_id, metadata.title, dest_dir)

    try:
        if source == "arxiv":
            _download_arxiv_pdf(metadata, paper_dir)
        else:
            _download_openreview_pdf(metadata, paper_dir)
    except Exception as e:
        output_error(f"Failed to download PDF: {e}")
        return

    tex_path = None
    if source == "arxiv":
        tex_path = _download_arxiv_latex(metadata, paper_dir)

    metadata_dict = asdict(metadata)
    metadata_dict["has_latex_source"] = tex_path is not None
    metadata_path = paper_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata_dict, ensure_ascii=False, indent=2))

    outputs = {
        "paper_dir": str(paper_dir),
        "pdf": str(paper_dir / "paper.pdf"),
        "metadata": str(metadata_path),
    }
    if tex_path:
        outputs["tex"] = str(tex_path)

    output_ok(
        f"Downloaded: {metadata.title}",
        paper_dir=str(paper_dir),
        outputs=outputs,
    )


if __name__ == "__main__":
    main()

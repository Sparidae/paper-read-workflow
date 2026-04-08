"""Arxiv paper downloader and metadata extractor."""

from __future__ import annotations

import re
import tarfile
import time
from pathlib import Path, PurePosixPath

import httpx

from paper_tool.downloaders.base import BaseDownloader
from paper_tool.models import PaperMetadata, PaperSource
from paper_tool.retry import retry as _retry

_ARXIV_ID_RE = re.compile(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?")


def _extract_arxiv_id(url: str) -> str:
    """
    Extract the Arxiv paper ID from various URL formats:
      - https://arxiv.org/abs/2301.00001
      - https://arxiv.org/abs/2301.00001v2
      - https://arxiv.org/pdf/2301.00001
      - https://arxiv.org/pdf/2301.00001v2
      - https://arxiv.org/pdf/2301.00001.pdf
      - https://alphaxiv.org/abs/2301.00001
      - https://alphaxiv.org/abs/2301.00001v2
      - https://huggingface.co/papers/2301.00001
      - https://ar5iv.labs.google.com/html/2301.00001
      - 2301.00001  (bare ID)
    Falls back to searching for an arxiv-style ID (YYMM.NNNNN) anywhere in the
    input string, so new hosting sites are handled automatically.
    """
    m = _ARXIV_ID_RE.search(url)
    if m:
        return m.group(0)
    raise ValueError(f"Could not extract Arxiv ID from URL: {url!r}")


def _paper_dir(metadata: "PaperMetadata", dest_dir: Path) -> Path:
    """
    Return (and create) the per-paper subdirectory:
        dest_dir / {paper_id}_{safe_title}/
    """
    safe_title = re.sub(r'[\\/*?:"<>|]', "", metadata.title)
    safe_title = safe_title.replace(" ", "_")[:80]
    paper_dir = dest_dir / f"{metadata.paper_id}_{safe_title}"
    paper_dir.mkdir(parents=True, exist_ok=True)
    return paper_dir


def _safe_member_relpath(name: str) -> Path | None:
    """Return a safe relative path for a tar member, or None if unusable."""
    parts = [part for part in PurePosixPath(name).parts if part not in ("", ".", "..")]
    if not parts:
        return None
    return Path(*parts)


def _normalize_tex_path(raw_path: str, base_dir: Path) -> Path:
    """Normalize an input/include path relative to the current tex file."""
    candidate = Path(raw_path.strip())
    if candidate.suffix != ".tex":
        candidate = candidate.with_suffix(".tex")

    normalized_parts: list[str] = []
    for part in (base_dir / candidate).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if normalized_parts:
                normalized_parts.pop()
            continue
        normalized_parts.append(part)
    return Path(*normalized_parts)


def _pick_root_tex(tex_files: dict[Path, str]) -> Path | None:
    """Pick the most likely root tex file for expansion."""
    candidates: list[Path] = []
    for path, content in tex_files.items():
        if "\\documentclass" in content and "\\begin{document}" in content:
            candidates.append(path)
    if not candidates:
        return None

    preferred_names = {"main.tex": 0, "paper.tex": 1}
    return min(
        candidates,
        key=lambda path: (
            preferred_names.get(path.name.lower(), 2),
            len(path.parts),
            len(path.as_posix()),
            path.as_posix(),
        ),
    )


def _expand_tex_includes(
    path: Path,
    tex_files: dict[Path, str],
    visited: set[Path] | None = None,
) -> str:
    """Expand input/include statements recursively in document order."""
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
    expanded_lines: list[str] = []

    for line in content.splitlines():
        match = include_re.match(line)
        if not match:
            expanded_lines.append(line)
            continue

        target_path = _normalize_tex_path(match.group("target"), path.parent)
        nested = tex_files.get(target_path)
        if nested is None:
            expanded_lines.append(line)
            continue

        prefix = match.group("prefix")
        if prefix.strip():
            expanded_lines.append(line)
            continue

        expanded_lines.append(f"% --- begin include: {target_path.as_posix()} ---")
        expanded_lines.append(_expand_tex_includes(target_path, tex_files, visited))
        expanded_lines.append(f"% --- end include: {target_path.as_posix()} ---")

    return "\n".join(expanded_lines)


class ArxivDownloader(BaseDownloader):
    """Downloads papers from arxiv.org using the arxiv Python package."""

    @_retry(max_attempts=3, base_delay=2.0)
    def fetch_metadata(self, url: str) -> PaperMetadata:
        from datetime import date
        from html.parser import HTMLParser

        paper_id = _extract_arxiv_id(url)
        base_id = re.sub(r"v\d+$", "", paper_id)

        abs_url = f"https://arxiv.org/abs/{base_id}"
        headers = {"User-Agent": "paper-tool/0.1"}

        with httpx.Client(follow_redirects=True, timeout=30) as client:
            response = client.get(abs_url, headers=headers)
            response.raise_for_status()

        html = response.text

        class _MetaParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.meta: dict[str, list[str]] = {}

            def handle_starttag(self, tag: str, attrs: list) -> None:
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

        published_date: date | None = None
        raw_date = (m.get("citation_date") or [""])[0]
        if raw_date:
            try:
                published_date = date.fromisoformat(raw_date.replace("/", "-"))
            except ValueError:
                pass

        # Extract category IDs like "cs.CV" from subjects cell
        subjects_match = re.search(
            r'class="tablecell subjects">(.*?)</td>', html, re.DOTALL
        )
        tags: list[str] = []
        if subjects_match:
            tags = re.findall(r"\(([a-z\-]+\.[A-Z]+)\)", subjects_match.group(1))

        canonical_url = f"https://arxiv.org/abs/{base_id}"

        return PaperMetadata(
            title=title,
            authors=authors,
            abstract=abstract,
            source=PaperSource.ARXIV,
            url=canonical_url,
            paper_id=base_id,
            published_date=published_date,
            tags=tags,
        )

    def download_pdf(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        """Download PDF via direct URL construction."""
        paper_dir = _paper_dir(metadata, dest_dir)
        dest_path = paper_dir / "paper.pdf"

        if dest_path.exists():
            return dest_path

        pdf_url = f"https://arxiv.org/pdf/{metadata.paper_id}.pdf"
        headers = {"User-Agent": "paper-tool/0.1"}

        with httpx.Client(follow_redirects=True, timeout=60) as client:
            for attempt in range(3):
                try:
                    with client.stream("GET", pdf_url, headers=headers) as response:
                        response.raise_for_status()
                        with open(dest_path, "wb") as f:
                            for chunk in response.iter_bytes(chunk_size=8192):
                                f.write(chunk)
                    return dest_path
                except httpx.HTTPStatusError as e:
                    if attempt == 2:
                        raise RuntimeError(
                            f"Failed to download PDF from {pdf_url}: {e}"
                        ) from e
                    time.sleep(2**attempt)

        return dest_path

    def download_latex_source(
        self, metadata: PaperMetadata, dest_dir: Path
    ) -> Path | None:
        """
        Download and extract the LaTeX source from Arxiv.
        Returns the path to a merged .tex file, or None if unavailable.

        Arxiv serves source as a tar.gz at https://arxiv.org/src/{id}.
        Some papers are PDF-only (no source available).

        As a side effect, image files (png/jpg/jpeg/pdf) are extracted into
        a figures/ subdirectory inside the paper's own directory.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        paper_dir = _paper_dir(metadata, dest_dir)
        merged_path = paper_dir / "paper.tex"
        figures_dir = paper_dir / "figures"
        source_dir = paper_dir / "source"

        if merged_path.exists() and figures_dir.exists() and source_dir.exists():
            return merged_path

        src_url = f"https://arxiv.org/src/{metadata.paper_id}"
        headers = {"User-Agent": "paper-tool/0.1"}

        _IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
        _MAX_IMG_BYTES = 25 * 1024 * 1024  # skip anything above 25 MB before conversion

        try:
            from paper_tool.retry import with_retry

            with httpx.Client(follow_redirects=True, timeout=60) as client:
                response = with_retry(
                    lambda: client.get(src_url, headers=headers),
                    max_attempts=3,
                    base_delay=2.0,
                )
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "pdf" in content_type or len(response.content) < 1000:
                    return None

                tar_path = dest_dir / f"{metadata.paper_id}_src.tar.gz"
                tar_path.write_bytes(response.content)

                try:
                    with tarfile.open(tar_path) as tar:
                        tex_contents: list[str] = []
                        tex_files: dict[Path, str] = {}
                        figures_dir.mkdir(parents=True, exist_ok=True)
                        source_dir.mkdir(parents=True, exist_ok=True)

                        for member in tar.getmembers():
                            rel_path = _safe_member_relpath(member.name)
                            if rel_path is None or not member.isfile():
                                continue

                            suffix = Path(member.name).suffix.lower()
                            f = tar.extractfile(member)
                            if not f:
                                continue

                            # Keep a full copy of the original source tree for robust
                            # LaTeX-based figure/table rendering.
                            source_out = source_dir / rel_path
                            source_out.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                content = f.read()
                            except Exception:
                                continue
                            try:
                                source_out.write_bytes(content)
                            except Exception:
                                pass

                            if member.name.endswith(".tex"):
                                try:
                                    decoded = content.decode("utf-8", errors="replace")
                                    tex_contents.append(decoded)
                                    tex_files[rel_path] = decoded
                                except Exception:
                                    pass

                            elif (
                                suffix in _IMG_SUFFIXES
                                and member.size <= _MAX_IMG_BYTES
                            ):
                                # Flatten filename and drop subdirectory path.
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
                            _expand_tex_includes(root_tex, tex_files)
                            if root_tex is not None
                            else ""
                        )
                        if not merged_tex.strip():
                            merged_tex = "\n\n% --- next file ---\n\n".join(
                                tex_contents
                            )

                        merged_path.write_text(merged_tex)
                finally:
                    tar_path.unlink(missing_ok=True)

            return merged_path
        except Exception:
            return None

    def get_figures_dir(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        """Return the figures directory path (may or may not exist yet)."""
        return _paper_dir(metadata, dest_dir) / "figures"

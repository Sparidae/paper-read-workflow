"""Arxiv paper downloader and metadata extractor."""

from __future__ import annotations

import re
import tarfile
import time
from pathlib import Path

import httpx

from paper_tool.downloaders.base import BaseDownloader
from paper_tool.models import PaperMetadata, PaperSource


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


class ArxivDownloader(BaseDownloader):
    """Downloads papers from arxiv.org using the arxiv Python package."""

    def fetch_metadata(self, url: str) -> PaperMetadata:
        import arxiv

        paper_id = _extract_arxiv_id(url)
        # Strip version suffix for search
        base_id = re.sub(r"v\d+$", "", paper_id)

        client = arxiv.Client()
        search = arxiv.Search(id_list=[base_id])
        results = list(client.results(search))

        if not results:
            raise ValueError(f"No Arxiv paper found for ID: {base_id!r}")

        result = results[0]

        tags = [cat for cat in result.categories]

        return PaperMetadata(
            title=result.title.strip(),
            authors=[str(a) for a in result.authors],
            abstract=result.summary.strip().replace("\n", " "),
            source=PaperSource.ARXIV,
            url=result.entry_id,
            paper_id=base_id,
            published_date=result.published.date() if result.published else None,
            tags=tags,
        )

    def download_pdf(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        """Download PDF via direct URL construction (faster than arxiv package download)."""
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
                    time.sleep(2 ** attempt)

        return dest_path

    def download_latex_source(self, metadata: PaperMetadata, dest_dir: Path) -> Path | None:
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

        if merged_path.exists() and figures_dir.exists():
            return merged_path

        src_url = f"https://arxiv.org/src/{metadata.paper_id}"
        headers = {"User-Agent": "paper-tool/0.1"}

        _IMG_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
        _MAX_IMG_BYTES = 25 * 1024 * 1024  # skip anything above 25 MB before conversion

        try:
            with httpx.Client(follow_redirects=True, timeout=60) as client:
                response = client.get(src_url, headers=headers)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                # Arxiv returns application/pdf when source is not available
                if "pdf" in content_type or len(response.content) < 1000:
                    return None

                tar_path = dest_dir / f"{metadata.paper_id}_src.tar.gz"
                tar_path.write_bytes(response.content)

                try:
                    with tarfile.open(tar_path) as tar:
                        tex_contents: list[str] = []
                        figures_dir.mkdir(parents=True, exist_ok=True)

                        for member in tar.getmembers():
                            suffix = Path(member.name).suffix.lower()

                            if member.name.endswith(".tex"):
                                f = tar.extractfile(member)
                                if f:
                                    try:
                                        tex_contents.append(
                                            f.read().decode("utf-8", errors="replace")
                                        )
                                    except Exception:
                                        pass

                            elif suffix in _IMG_SUFFIXES and member.size <= _MAX_IMG_BYTES:
                                # Flatten: save only the filename, drop subdirectory path
                                fname = Path(member.name).name
                                out_path = figures_dir / fname
                                if not out_path.exists():
                                    f = tar.extractfile(member)
                                    if f:
                                        try:
                                            out_path.write_bytes(f.read())
                                        except Exception:
                                            pass

                        if not tex_contents:
                            return None
                        merged_path.write_text(
                            "\n\n% --- next file ---\n\n".join(tex_contents)
                        )
                finally:
                    tar_path.unlink(missing_ok=True)

            return merged_path
        except Exception:
            return None

    def get_figures_dir(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        """Return the figures directory path (may or may not exist yet)."""
        return _paper_dir(metadata, dest_dir) / "figures"

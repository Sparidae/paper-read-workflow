"""OpenReview paper downloader and metadata extractor."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from paper_tool.downloaders.base import BaseDownloader
from paper_tool.models import PaperMetadata, PaperSource
from paper_tool.retry import retry as _retry


def _extract_forum_id(url: str) -> str:
    """
    Extract forum/note ID from OpenReview URLs:
      - https://openreview.net/forum?id=XXXXX
      - https://openreview.net/pdf?id=XXXXX
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "id" in params:
        return params["id"][0]
    raise ValueError(f"Could not extract OpenReview paper ID from URL: {url!r}")


class OpenReviewDownloader(BaseDownloader):
    """Downloads papers from openreview.net using the openreview-py package."""

    def _get_client(self) -> "openreview.api.OpenReviewClient":
        import openreview

        from paper_tool.config import get_config

        cfg = get_config()
        kwargs: dict = {"baseurl": "https://api2.openreview.net"}
        if cfg.openreview_username and cfg.openreview_password:
            kwargs["username"] = cfg.openreview_username
            kwargs["password"] = cfg.openreview_password
        return openreview.api.OpenReviewClient(**kwargs)

    @_retry(max_attempts=3, base_delay=2.0)
    def fetch_metadata(self, url: str) -> PaperMetadata:
        forum_id = _extract_forum_id(url)
        client = self._get_client()

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

        # Try to get publication date from tcdate
        from datetime import date

        pub_date: date | None = None
        if hasattr(note, "tcdate") and note.tcdate:
            try:
                from datetime import datetime

                pub_date = datetime.fromtimestamp(note.tcdate / 1000).date()
            except Exception:
                pass

        canonical_url = f"https://openreview.net/forum?id={forum_id}"

        return PaperMetadata(
            title=title,
            authors=authors,
            abstract=abstract,
            source=PaperSource.OPENREVIEW,
            url=canonical_url,
            paper_id=forum_id,
            published_date=pub_date,
            tags=tags,
        )

    def download_pdf(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)

        safe_title = re.sub(r'[\\/*?:"<>|]', "", metadata.title)
        safe_title = safe_title.replace(" ", "_")[:80]
        paper_dir = dest_dir / f"OR_{metadata.paper_id}_{safe_title}"
        paper_dir.mkdir(parents=True, exist_ok=True)
        dest_path = paper_dir / "paper.pdf"

        if dest_path.exists():
            return dest_path

        pdf_url = f"https://openreview.net/pdf?id={metadata.paper_id}"
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

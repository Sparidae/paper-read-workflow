"""Paper downloaders for Arxiv and OpenReview."""

from paper_tool.downloaders.base import BaseDownloader
from paper_tool.downloaders.arxiv import ArxivDownloader
from paper_tool.downloaders.openreview import OpenReviewDownloader

__all__ = ["BaseDownloader", "ArxivDownloader", "OpenReviewDownloader"]


def get_downloader(url: str) -> BaseDownloader:
    """Auto-detect paper source from URL and return the appropriate downloader."""
    url_lower = url.lower()
    if "arxiv.org" in url_lower or "alphaxiv.org" in url_lower:
        return ArxivDownloader()
    if "openreview.net" in url_lower:
        return OpenReviewDownloader()
    raise ValueError(
        f"Unsupported URL: {url!r}\n"
        "Supported sources: arxiv.org, alphaxiv.org, openreview.net"
    )

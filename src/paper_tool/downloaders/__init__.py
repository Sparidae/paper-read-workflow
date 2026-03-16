"""Paper downloaders for Arxiv and OpenReview."""

from paper_tool.downloaders.base import BaseDownloader
from paper_tool.downloaders.arxiv import ArxivDownloader
from paper_tool.downloaders.openreview import OpenReviewDownloader

__all__ = ["BaseDownloader", "ArxivDownloader", "OpenReviewDownloader"]


_ARXIV_DOMAINS = {"arxiv.org", "alphaxiv.org", "ar5iv.labs.google.com"}
_ARXIV_ID_RE = __import__("re").compile(r"[0-9]{4}\.[0-9]{4,5}(?:v\d+)?")


def _looks_like_arxiv(url: str) -> bool:
    """Return True if the URL points to an arxiv paper (known hosts or HF papers)."""
    url_lower = url.lower()
    if any(domain in url_lower for domain in _ARXIV_DOMAINS):
        return True
    if "huggingface.co/papers/" in url_lower:
        return True
    if _ARXIV_ID_RE.fullmatch(url.strip()):
        return True
    return False


def get_downloader(url: str) -> BaseDownloader:
    """Auto-detect paper source from URL and return the appropriate downloader."""
    url_lower = url.lower()
    if _looks_like_arxiv(url):
        return ArxivDownloader()
    if "openreview.net" in url_lower:
        return OpenReviewDownloader()
    raise ValueError(
        f"Unsupported URL: {url!r}\n"
        "Supported: arxiv.org, alphaxiv.org, huggingface.co/papers, "
        "ar5iv.labs.google.com, openreview.net, or bare arxiv ID"
    )

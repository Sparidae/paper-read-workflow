"""Abstract base class for paper downloaders."""

from abc import ABC, abstractmethod
from pathlib import Path

from paper_tool.models import PaperMetadata


class BaseDownloader(ABC):
    """All downloaders must implement fetch_metadata and download_pdf."""

    @abstractmethod
    def fetch_metadata(self, url: str) -> PaperMetadata:
        """Fetch paper metadata (title, authors, abstract, etc.) from the source."""
        ...

    @abstractmethod
    def download_pdf(self, metadata: PaperMetadata, dest_dir: Path) -> Path:
        """Download the PDF to dest_dir and return the local file path."""
        ...

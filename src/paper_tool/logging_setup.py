"""Centralized logging configuration for paper-tool.

Writes DEBUG-level logs to a rotating file (logs/paper_tool.log) next to
config.yaml.  No console handler is added — the terminal remains clean.
Call setup_logging() once at CLI startup; subsequent calls are no-ops.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_initialized = False


def setup_logging(log_dir: Path | None = None) -> None:
    """Configure root logger with a rotating file handler.

    Parameters
    ----------
    log_dir:
        Directory for the log file.  Defaults to ``logs/`` next to the
        project's config.yaml (i.e. PROJECT_ROOT/logs/).
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if log_dir is None:
        from paper_tool.config import PROJECT_ROOT

        log_dir = PROJECT_ROOT / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "paper_tool.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-35s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "notion_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug("Logging initialised → %s", log_file)

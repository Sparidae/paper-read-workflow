"""Centralized logging configuration for paper-tool.

Writes DEBUG-level logs to a rotating file (logs/paper_tool.log) next to
config.yaml.  No console handler is added — the terminal remains clean.
Call setup_logging() once at CLI startup; subsequent calls are no-ops.
Old backup files are automatically deleted on startup (default: 30 days).
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path

_initialized = False

_LOG_STEM = "paper_tool.log"


def _cleanup_old_logs(log_dir: Path, keep_days: int) -> int:
    """Delete rotated backup log files older than *keep_days* days.

    Only touches files matching ``paper_tool.log.*`` (the numbered backups
    produced by RotatingFileHandler).  The active ``paper_tool.log`` is
    never deleted.  Returns the number of files removed.
    """
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in log_dir.glob(f"{_LOG_STEM}.*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def setup_logging(log_dir: Path | None = None, keep_days: int = 30) -> None:
    """Configure root logger with a rotating file handler.

    Parameters
    ----------
    log_dir:
        Directory for the log file.  Defaults to ``logs/`` next to the
        project's config.yaml (i.e. PROJECT_ROOT/logs/).
    keep_days:
        Rotated backup files older than this many days are deleted on startup.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if log_dir is None:
        from paper_tool.config import PROJECT_ROOT

        log_dir = PROJECT_ROOT / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)

    removed = _cleanup_old_logs(log_dir, keep_days)

    log_file = log_dir / _LOG_STEM

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
    handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "notion_client", "LiteLLM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger(__name__)
    log.debug("Logging initialised → %s", log_file)
    if removed:
        log.debug("Cleaned up %d log backup(s) older than %d days", removed, keep_days)

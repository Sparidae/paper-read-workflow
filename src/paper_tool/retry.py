"""Reusable retry utilities with exponential backoff."""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


def retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
) -> Callable:
    """
    Decorator that retries on any ``Exception`` with exponential backoff.

    Usage::

        @retry(max_attempts=3, base_delay=2.0)
        def flaky_call():
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        raise
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    log.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        func.__qualname__,
                        attempt + 1,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


def with_retry(
    fn: Callable[[], T],
    /,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
) -> T:
    """
    Call *fn()* with retry on failure.

    Use a lambda or ``functools.partial`` to capture arguments::

        response = with_retry(lambda: litellm.completion(**kwargs), max_attempts=3)
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                getattr(fn, "__qualname__", str(fn)),
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]

"""Helpers for LLM streaming output in a fixed-height Rich panel."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CompletionResult:
    """Normalized LLM completion result across streaming and non-streaming modes."""

    text: str
    finish_reason: str | None = None
    usage: Any = None


class StreamWindow:
    """A small fixed-height live panel for token streaming output."""

    def __init__(
        self, title: str, *, height: int = 8, max_buffer_chars: int = 8000
    ) -> None:
        from rich.console import Console
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text

        self._Console = Console
        self._Live = Live
        self._Panel = Panel
        self._Text = Text

        self._title = title
        self._height = max(4, height)
        self._max_buffer_chars = max(2000, max_buffer_chars)
        self._buffer = ""
        self._live = None
        self._last_refresh = 0.0

    def __enter__(self) -> "StreamWindow":
        # Use stderr so it does not collide with the primary stdout progress area.
        console = self._Console(stderr=True)
        self._live = self._Live(
            self._renderable("等待模型输出..."),
            console=console,
            refresh_per_second=15,
            transient=True,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def append(self, text: str) -> None:
        if not text:
            return

        self._buffer += text
        if len(self._buffer) > self._max_buffer_chars:
            self._buffer = self._buffer[-self._max_buffer_chars :]

        # Throttle live updates to reduce terminal flicker and CPU overhead.
        now = time.monotonic()
        if (now - self._last_refresh) < 0.03:
            return
        self._last_refresh = now
        self.refresh()

    def refresh(self) -> None:
        if self._live is None:
            return
        shown = (
            self._buffer[-self._max_buffer_chars :]
            if self._buffer
            else "等待模型输出..."
        )
        self._live.update(self._renderable(shown))

    def _renderable(self, content: str):
        return self._Panel(
            self._Text(content),
            title=self._title,
            height=self._height,
            border_style="cyan",
        )


def _join_message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict):
                # OpenAI-style multimodal chunks often carry text here.
                txt = item.get("text")
                if isinstance(txt, str):
                    chunks.append(txt)
                    continue
                inner = item.get("content")
                if isinstance(inner, str):
                    chunks.append(inner)
        return "".join(chunks)
    return str(content)


def _extract_final_text(choice_message: Any) -> str:
    raw = _join_message_text(getattr(choice_message, "content", None))
    if raw.strip():
        return raw
    reasoning = _join_message_text(getattr(choice_message, "reasoning_content", None))
    return reasoning


def _extract_delta_text(choice_delta: Any) -> str:
    if choice_delta is None:
        return ""

    content = _join_message_text(getattr(choice_delta, "content", None))
    if content:
        return content

    reasoning = _join_message_text(getattr(choice_delta, "reasoning_content", None))
    if reasoning:
        return reasoning

    # Some SDK adapters expose dict-like delta objects.
    if isinstance(choice_delta, dict):
        content = _join_message_text(choice_delta.get("content"))
        if content:
            return content
        reasoning = _join_message_text(choice_delta.get("reasoning_content"))
        if reasoning:
            return reasoning

    return ""


_text_client: Any = None
_vision_client: Any = None


def _get_client(
    use_vision: bool = False,
    vision_api_key: str | None = None,
    vision_base_url: str | None = None,
) -> Any:
    """Lazily create and cache OpenAI clients.

    Caching is per-(api_key, base_url) so different credentials get different clients.
    """
    from openai import OpenAI

    global _text_client, _vision_client
    if use_vision:
        if vision_api_key is None:
            vision_api_key = os.getenv("OPENAI_VISION_API_KEY") or os.getenv(
                "OPENAI_API_KEY"
            )
        if vision_base_url is None:
            vision_base_url = os.getenv("OPENAI_VISION_BASE_URL") or os.getenv(
                "OPENAI_BASE_URL"
            )
        cache_key = (vision_api_key, vision_base_url)
        if (
            _vision_client is None
            or getattr(_vision_client, "_cache_key", None) != cache_key
        ):
            kwargs: dict[str, Any] = {}
            if vision_api_key:
                kwargs["api_key"] = vision_api_key
            if vision_base_url:
                kwargs["base_url"] = vision_base_url
            client = OpenAI(**kwargs)
            client._cache_key = cache_key  # type: ignore[attr-defined]
            _vision_client = client
        return _vision_client

    if _text_client is None:
        _text_client = OpenAI()
    return _text_client


def completion_to_text(
    *,
    request_kwargs: dict[str, Any],
    stream: bool = False,
    stream_title: str = "",
    stream_height: int = 8,
    on_token: Callable[[str], None] | None = None,
    use_vision: bool = False,
    vision_api_key: str | None = None,
    vision_base_url: str | None = None,
) -> CompletionResult:
    """
    Run OpenAI completion and normalize text extraction.

    Three modes:
    - on_token provided  → stream from OpenAI, deliver each token via on_token
                           callback, no StreamWindow (pipeline.py web/headless)
    - stream=True        → stream from OpenAI into a Rich StreamWindow (CLI)
    - stream=False       → non-streaming OpenAI call

    Set use_vision=True to use the vision endpoint.
    Pass vision_api_key / vision_base_url explicitly, or they default
    to the OPENAI_VISION_* env vars.
    """
    _client = _get_client(use_vision, vision_api_key, vision_base_url)

    # ── Mode 1: callback-based streaming (no terminal UI) ─────────────────────
    if on_token is not None:
        stream_kwargs = dict(request_kwargs)
        stream_kwargs["stream"] = True

        all_parts: list[str] = []
        content_parts: list[str] = []  # non-reasoning content only
        finish_reason: str | None = None

        for chunk in _client.chat.completions.create(**stream_kwargs):
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            piece = _extract_delta_text(delta)
            if piece:
                all_parts.append(piece)
                on_token(piece)
            # Track actual content separately (excludes reasoning_content)
            if delta is not None:
                content_piece = _join_message_text(
                    getattr(delta, "content", None)
                    if not isinstance(delta, dict)
                    else delta.get("content")
                )
                if content_piece:
                    content_parts.append(content_piece)
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr

        # Use content-only text for callers that parse structured output (e.g. JSON).
        # Fall back to all_parts if no explicit content (non-thinking models).
        result_text = "".join(content_parts).strip() or "".join(all_parts).strip()
        return CompletionResult(
            text=result_text,
            finish_reason=finish_reason,
            usage=None,
        )

    # ── Mode 2: non-streaming ──────────────────────────────────────────────────
    if not stream:
        response = _client.chat.completions.create(**request_kwargs)
        choice = response.choices[0]
        raw = _extract_final_text(choice.message).strip()
        return CompletionResult(
            text=raw,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=getattr(response, "usage", None),
        )

    # ── Mode 3: StreamWindow (CLI) ─────────────────────────────────────────────
    stream_kwargs = dict(request_kwargs)
    stream_kwargs["stream"] = True

    parts = []
    finish_reason = None

    with StreamWindow(stream_title, height=stream_height) as window:
        for chunk in _client.chat.completions.create(**stream_kwargs):
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            piece = _extract_delta_text(delta)
            if piece:
                parts.append(piece)
                window.append(piece)
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
        window.refresh()

    return CompletionResult(
        text="".join(parts).strip(),
        finish_reason=finish_reason,
        usage=None,
    )

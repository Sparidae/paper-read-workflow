"""Helpers for LLM streaming output in a fixed-height Rich panel."""

from __future__ import annotations

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


def completion_to_text(
    *,
    request_kwargs: dict[str, Any],
    stream: bool = False,
    stream_title: str = "",
    stream_height: int = 8,
    on_token: Callable[[str], None] | None = None,
) -> CompletionResult:
    """
    Run litellm completion and normalize text extraction.

    Three modes:
    - on_token provided  → stream from litellm, deliver each token via on_token callback,
                           no StreamWindow (used by pipeline.py for web/headless callers)
    - stream=True        → stream from litellm into a Rich StreamWindow (CLI use)
    - stream=False       → non-streaming litellm call
    """
    import litellm

    # ── Mode 1: callback-based streaming (no terminal UI) ─────────────────────
    if on_token is not None:
        stream_kwargs = dict(request_kwargs)
        stream_kwargs["stream"] = True

        all_parts: list[str] = []
        content_parts: list[str] = []  # non-reasoning content only
        finish_reason: str | None = None

        for chunk in litellm.completion(**stream_kwargs):
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
        response = litellm.completion(**request_kwargs)
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
        for chunk in litellm.completion(**stream_kwargs):
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

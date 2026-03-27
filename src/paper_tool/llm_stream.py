"""Helpers for LLM streaming output with Rich-native incremental rendering."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CompletionResult:
    """Normalized LLM completion result across streaming and non-streaming modes."""

    text: str
    finish_reason: str | None = None
    usage: Any = None


class StreamPrinter:
    """Rich-native incremental printer for streamed token output."""

    def __init__(
        self,
        title: str,
        *,
        height: int = 8,
        max_buffer_chars: int = 8000,
        refresh_interval: float = 0.12,
    ) -> None:
        from rich.console import Console

        self._Console = Console

        self._title = title
        self._height = max(4, height)  # kept for backward-compatible signature
        self._max_buffer_chars = max(2000, max_buffer_chars)  # idem
        self._refresh_interval = max(0.05, refresh_interval)
        self._console = None
        self._pending = ""
        self._last_refresh = 0.0
        self._last_char = ""
        self._has_output = False

    def __enter__(self) -> "StreamPrinter":
        # Use stderr so it does not collide with the primary stdout progress bar.
        self._console = self._Console(stderr=True)
        self._console.rule(f"[bold cyan]{self._title}[/bold cyan]")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.flush()
        if self._console is not None and self._has_output and self._last_char != "\n":
            self._console.print()
        self._console = None

    def append(self, text: str) -> None:
        if not text:
            return

        self._pending += text
        self._has_output = True
        self._last_char = text[-1]

        # Flush in readable chunks instead of token-by-token repaint.
        now = time.monotonic()
        if not self._should_flush(text, now):
            return
        self.flush(now=now)

    def flush(self, *, now: float | None = None) -> None:
        if not self._pending or self._console is None:
            return
        self._console.print(self._pending, end="", markup=False, highlight=False, soft_wrap=True)
        self._pending = ""
        self._last_refresh = now if now is not None else time.monotonic()

    def _should_flush(self, text: str, now: float) -> bool:
        if "\n" in text:
            return True
        if any(ch in text for ch in "。！？!?;；,.，"):
            return True
        if len(self._pending) >= 64:
            return True
        return (now - self._last_refresh) >= self._refresh_interval


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
    stream: bool,
    stream_title: str,
    stream_height: int = 8,
) -> CompletionResult:
    """
    Run litellm completion and normalize text extraction.

    In stream mode, tokens are incrementally printed with Rich.
    """
    import litellm

    if not stream:
        response = litellm.completion(**request_kwargs)
        choice = response.choices[0]
        raw = _extract_final_text(choice.message).strip()
        return CompletionResult(
            text=raw,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=getattr(response, "usage", None),
        )

    stream_kwargs = dict(request_kwargs)
    stream_kwargs["stream"] = True

    parts: list[str] = []
    finish_reason: str | None = None

    with StreamPrinter(stream_title, height=stream_height) as printer:
        for chunk in litellm.completion(**stream_kwargs):
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            piece = _extract_delta_text(delta)
            if piece:
                parts.append(piece)
                printer.append(piece)
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
        printer.flush()

    return CompletionResult(
        text="".join(parts).strip(),
        finish_reason=finish_reason,
        usage=None,
    )

"""FastAPI web server for paper-tool frontend."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from paper_tool.logging_setup import setup_logging

setup_logging()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="paper-tool")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


class _Cancelled(Exception):
    """Raised inside the pipeline thread when the user requests a stop."""


@app.websocket("/ws/process")
async def ws_process(websocket: WebSocket) -> None:
    await websocket.accept()
    cancel_event: threading.Event | None = None
    pipeline_task: asyncio.Task | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "message": "无效的 JSON 消息"}
                )
                continue

            action = msg.get("action")

            if action == "stop":
                if cancel_event:
                    cancel_event.set()
                # pipeline_task will notice via on_event and terminate gracefully
                continue

            if action != "add":
                await websocket.send_json({"type": "error", "message": "未知操作"})
                continue

            url = (msg.get("url") or "").strip()
            if not url:
                await websocket.send_json({"type": "error", "message": "URL 不能为空"})
                continue

            # Cancel any previous in-flight pipeline before starting a new one
            if cancel_event:
                cancel_event.set()
            if pipeline_task and not pipeline_task.done():
                pipeline_task.cancel()

            cancel_event = threading.Event()
            pipeline_task = asyncio.create_task(
                _run_pipeline(websocket, url, cancel_event)
            )

    except WebSocketDisconnect:
        if cancel_event:
            cancel_event.set()
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()


async def _run_pipeline(
    websocket: WebSocket, url: str, cancel_event: threading.Event
) -> None:
    """Run pipeline in a thread pool and stream all events to the WebSocket."""
    from paper_tool.pipeline import run_pipeline

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def on_event(event: dict) -> None:
        if cancel_event.is_set():
            raise _Cancelled()
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_in_thread() -> None:
        try:
            run_pipeline(
                url,
                skip_llm=False,
                debug=False,
                force=False,
                on_event=on_event,
                on_confirm_force=lambda _msg: True,
            )
        except _Cancelled:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "done", "success": False, "stopped": True},
            )
        except Exception as e:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "message": str(e)}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    future = loop.run_in_executor(None, run_in_thread)

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            try:
                await websocket.send_json(event)
            except Exception:
                cancel_event.set()
                break
    except asyncio.CancelledError:
        cancel_event.set()
        raise

    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=5)
    except (asyncio.TimeoutError, Exception):
        pass

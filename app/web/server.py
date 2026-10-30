"""The web page's server: FastAPI, one WebSocket, the engine behind it.

Browser  ──ws──>  {"type": "send", "text": "biryani"}    a new request or an answer
                  {"type": "new"}                        forget the unfinished request
Browser  <──ws──  every engine event (log / question / image / done / error / busy),
                  plus {"type": "hello", "history": [...]} on connect so a reload shows
                  the whole conversation again.

Listens on 127.0.0.1 only: the page can place orders, so nobody else on the Wi-Fi gets it.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.config import resolve

STATIC = Path(__file__).resolve().parent / "static"
MAX_HISTORY = 1500


class EventHub:
    """Fans engine events out to every open tab, and keeps them for page reloads."""

    def __init__(self, echo: bool = True):
        self.engine = None                       # set once the engine exists
        self.history: list[dict[str, Any]] = []
        self.queues: set[asyncio.Queue] = set()
        self.echo = echo

    def publish(self, event: dict[str, Any]) -> None:
        if event["type"] != "busy":
            self.history.append(event)
            del self.history[:-MAX_HISTORY]
        if self.echo and event["type"] in ("log", "error", "done"):
            print(event["text"])                 # the terminal shows progress too
        for q in list(self.queues):
            q.put_nowait(event)


def create_app(hub: EventHub) -> FastAPI:
    app = FastAPI(title="AR7 Shopping Agent")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/qr/{name}")
    async def qr(name: str) -> FileResponse:
        if not re.fullmatch(r"pay_[a-z_]+\.png", name):
            raise HTTPException(404)
        path = resolve(".state") / name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        engine = hub.engine
        queue: asyncio.Queue = asyncio.Queue()
        await socket.send_json({
            "type": "hello", "history": hub.history,
            "busy": bool(engine and engine.busy),
            "question": engine.question if engine else None,
            "unfinished": (engine.thread or {}).get("message") if engine and engine.question else None,
        })
        hub.queues.add(queue)

        async def writer() -> None:                 # one writer per tab keeps events in order
            while True:
                await socket.send_json(await queue.get())

        writing = asyncio.create_task(writer())
        try:
            while True:
                msg: dict[str, Any] = await socket.receive_json()
                if engine is None:
                    continue
                if msg.get("type") == "send":
                    asyncio.create_task(engine.send(str(msg.get("text", ""))))
                elif msg.get("type") == "new" and not engine.busy:
                    engine.abandon()
                    hub.publish({"type": "done", "text": "Started over. What do you want?"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.queues.discard(queue)
            writing.cancel()

    return app
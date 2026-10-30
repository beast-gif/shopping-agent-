"""The chat engine: connects the apps, runs the main graph, remembers the unfinished request.
Used by both front ends: scripts/chat.py (terminal) and scripts/web.py (web page).

Everything the agent wants to show goes out as an EVENT through `emit`:
    {"type": "user",     "text": "biryani"}                 what you sent
    {"type": "log",      "text": "Searching Swiggy..."}     progress lines from the graphs
    {"type": "question", "prompt": "...", "options": [...], "typed": "confirm 133"?}
    {"type": "image",    "url": "/qr/pay_zepto.png"}         a UPI QR to scan
    {"type": "done",     "text": "Paid. Zepto order is placed."}
    {"type": "error",    "text": "..."}
    {"type": "busy",     "value": true/false}
The terminal prints them; the web page draws them.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import AsyncExitStack
from typing import Any, Callable, Optional

from langgraph.types import Command

from app.adapters.amazon_now import AmazonNowAdapter
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.zepto import ZeptoAdapter
from app.adapters.zomato import ZomatoAdapter
from app.browser import amazon_browser, signed_in_name
from app.config import AppConfig, resolve
from app.mcp_client import McpHub, describe_error
from app.workflows.main import HELP, build_main_graph

Event = dict[str, Any]
THREAD_FILE = resolve(".state/chat_thread.json")      # the unfinished request, if any
_QR_LINE = re.compile(r"QR code:\s*(.+pay_[a-z_]+\.png)", re.IGNORECASE)


class Services:
    """The apps the graphs talk to. MCP servers connect in connect(), which must run in the
    same asyncio task that later closes the stack (MCP's rule). The Amazon browser opens
    lazily, the first time a grocery request needs it."""

    def __init__(self, cfg: AppConfig, use_amazon: bool = True, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.use_amazon = use_amazon
        self.log = log
        self.food: dict[str, Any] = {}
        self.grocery: dict[str, Any] = {}
        self._amazon_cm = None
        self._amazon_tried = False

    async def connect(self, stack: AsyncExitStack) -> None:
        hub = await stack.enter_async_context(McpHub(self.cfg))
        names = [n for n in ("zepto", "swiggy_food", "zomato")
                 if n in self.cfg.mcp.servers and self.cfg.mcp.servers[n].enabled]
        self.log("Connecting " + ", ".join(names) + "...")
        errors = await hub.connect_all(names)
        for server, err in errors.items():
            self.log(f"  [{server}] not connected: {err}")
        if "zepto" in names and "zepto" not in errors:
            self.grocery["zepto"] = ZeptoAdapter(hub)
        if "swiggy_food" in names and "swiggy_food" not in errors:
            self.food["swiggy"] = SwiggyAdapter(hub)
        if "zomato" in names and "zomato" not in errors:
            self.food["zomato"] = ZomatoAdapter(hub)
        stack.push_async_callback(self.close)

    async def ensure_grocery(self) -> None:
        """Opens Brave for Amazon Now once. Safe to call again (it does nothing then)."""
        if self._amazon_tried or not self.use_amazon:
            return
        self._amazon_tried = True
        self.log("Opening Amazon Now (Brave)...")
        try:
            cm = amazon_browser(self.cfg)
            context = await cm.__aenter__()
            self._amazon_cm = cm
            if await signed_in_name(context, self.cfg.amazon_now.base_url):
                self.grocery["amazon_now"] = AmazonNowAdapter(context, self.cfg)
            else:
                self.log("  Amazon not signed in (run scripts/amazon_login.py); using Zepto only.")
        except Exception as e:  # noqa: BLE001
            self.log(f"  Amazon Now unavailable: {describe_error(e)}")

    async def close(self) -> None:
        if self._amazon_cm is not None:
            try:
                await self._amazon_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._amazon_cm = None


def pending_question(snapshot) -> Optional[dict]:
    """The question the graph is waiting on (from a subgraph too), or None if it finished."""
    for task in snapshot.tasks or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return intr.value
    for intr in getattr(snapshot, "interrupts", ()) or ():
        return intr.value
    return None


class Engine:
    """One conversation at a time (it's your personal agent). send() starts a new request or
    answers the question the current one is waiting on."""

    def __init__(self, cfg: AppConfig, services: Services, saver, emit: Callable[[Event], None]):
        self.cfg = cfg
        self.emit = emit
        services.log = self._log
        self.app = build_main_graph(cfg, services.food, services.grocery,
                                    ensure_grocery=services.ensure_grocery, log=self._log).compile(checkpointer=saver)
        self.thread: Optional[dict[str, str]] = self._load_thread()
        self.question: Optional[dict] = None      # what we're waiting on, if anything
        self.lock = asyncio.Lock()

    # ---- events

    def _log(self, text: str) -> None:
        self.emit({"type": "log", "text": text})
        m = _QR_LINE.search(text)                  # payment.show_payment printed the QR's path
        if m:
            self.emit({"type": "image", "url": f"/qr/{m.group(1).replace(chr(92), '/').rsplit('/', 1)[-1]}"})

    # ---- the unfinished request (survives restarts)

    @staticmethod
    def _load_thread() -> Optional[dict[str, str]]:
        try:
            return json.loads(THREAD_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _save_thread(self) -> None:
        THREAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        THREAD_FILE.write_text(json.dumps(self.thread), encoding="utf-8")

    def _clear_thread(self) -> None:
        self.thread, self.question = None, None
        try:
            THREAD_FILE.unlink()
        except OSError:
            pass

    def _config(self) -> dict:
        return {"configurable": {"thread_id": self.thread["thread_id"]}, "recursion_limit": 200}

    async def restore(self) -> Optional[dict]:
        """After a restart: the question the saved request was waiting on (or None)."""
        if not self.thread:
            return None
        try:
            self.question = pending_question(await self.app.aget_state(self._config()))
        except Exception:  # noqa: BLE001
            self.question = None
        if self.question is None:
            self._clear_thread()
        return self.question

    def abandon(self) -> None:
        """Forget the unfinished request ('Start over')."""
        self._clear_thread()

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    # ---- the one entry point

    async def send(self, text: str) -> None:
        text = (text or "").strip()
        if self.lock.locked():
            self.emit({"type": "error", "text": "Still working on the last step; wait for it to finish."})
            return
        async with self.lock:
            self.emit({"type": "busy", "value": True})
            try:
                if self.question is not None and self.thread:
                    self.emit({"type": "user", "text": text})
                    await self._run(Command(resume=text))
                elif not text:
                    return
                elif text.lower() in ("help", "?"):
                    self.emit({"type": "user", "text": text})
                    self.emit({"type": "done", "text": HELP})
                else:
                    self.emit({"type": "user", "text": text})
                    self.thread = {"thread_id": uuid.uuid4().hex, "message": text}
                    self._save_thread()
                    await self._run({"user_message": text})
            except Exception as e:  # noqa: BLE001  one failed request doesn't end the agent
                self.emit({"type": "error", "text": f"That request failed: {describe_error(e)}"})
                self._clear_thread()
            finally:
                self.emit({"type": "busy", "value": False})

    async def _run(self, step: Any) -> None:
        async for _ in self.app.astream(step, self._config(), stream_mode="updates"):
            pass
        snapshot = await self.app.aget_state(self._config())
        self.question = pending_question(snapshot)
        if self.question is not None:
            self.emit({"type": "question", **self.question})
            return
        self.emit({"type": "done", "text": snapshot.values.get("result") or "Done."})
        self._clear_thread()
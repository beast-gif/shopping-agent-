"""The shopping agent as one chat: food and groceries, with memory on disk.

Run from the project root:
    python scripts/chat.py
    python scripts/chat.py --no-amazon        # never open the browser (Zepto only for groceries)

Then type what you want:
    biryani
    meghana foods
    guilt free ice cream under 200
    ice cream, chips, curd
    chicken momos at college
    help / quit

How it works:
- Zepto, Swiggy and Zomato connect once at start (saved logins, no browser).
- Brave (Amazon Now) opens only with your first grocery request.
- Every request is a LangGraph thread saved in .checkpoints/shop.sqlite. If you close the
  terminal in the middle (Ctrl+C), the next start offers to continue right at the question
  you were on.
- Nothing is ordered unless you type the exact 'confirm <total>' and pay (or press
  'Proceed to pay' yourself on Amazon).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.adapters.amazon_now import AmazonNowAdapter  # noqa: E402
from app.adapters.swiggy import SwiggyAdapter  # noqa: E402
from app.adapters.zepto import ZeptoAdapter  # noqa: E402
from app.adapters.zomato import ZomatoAdapter  # noqa: E402
from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import AppConfig, load_config, resolve  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.workflows.main import HELP, build_main_graph  # noqa: E402

THREAD_FILE = resolve(".state/chat_thread.json")      # the unfinished request, if any
QUIT = {"quit", "exit", "bye", "q"}


class Services:
    """The apps the graphs talk to. MCP servers connect here, in the main task (their
    connections must be opened and closed in the same task). The Amazon browser opens
    lazily, the first time a grocery request needs it."""

    def __init__(self, cfg: AppConfig, use_amazon: bool):
        self.cfg = cfg
        self.use_amazon = use_amazon
        self.food: dict[str, Any] = {}
        self.grocery: dict[str, Any] = {}
        self._amazon_cm = None
        self._amazon_tried = False

    async def connect(self, stack: AsyncExitStack) -> None:
        hub = await stack.enter_async_context(McpHub(self.cfg))
        names = [n for n in ("zepto", "swiggy_food", "zomato") if n in self.cfg.mcp.servers
                 and self.cfg.mcp.servers[n].enabled]
        print("Connecting " + ", ".join(names) + "...")
        errors = await hub.connect_all(names)
        for server, err in errors.items():
            print(f"  [{server}] not connected: {err}")
        if "zepto" in names and "zepto" not in errors:
            self.grocery["zepto"] = ZeptoAdapter(hub)
        if "swiggy_food" in names and "swiggy_food" not in errors:
            self.food["swiggy"] = SwiggyAdapter(hub)
        if "zomato" in names and "zomato" not in errors:
            self.food["zomato"] = ZomatoAdapter(hub)

    async def ensure_grocery(self) -> None:
        """Opens Brave for Amazon Now once. Safe to call again (it does nothing then)."""
        if self._amazon_tried or not self.use_amazon:
            return
        self._amazon_tried = True
        print("Opening Amazon Now (Brave)...")
        try:
            cm = amazon_browser(self.cfg)
            context = await cm.__aenter__()
            self._amazon_cm = cm
            if await signed_in_name(context, self.cfg.amazon_now.base_url):
                self.grocery["amazon_now"] = AmazonNowAdapter(context, self.cfg)
            else:
                print("  Amazon not signed in (run scripts/amazon_login.py); using Zepto only.")
        except Exception as e:  # noqa: BLE001
            print(f"  Amazon Now unavailable: {describe_error(e)}")

    async def close(self) -> None:
        if self._amazon_cm is not None:
            try:
                await self._amazon_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- the unfinished request


def load_thread() -> Optional[dict[str, str]]:
    try:
        return json.loads(THREAD_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_thread(thread: dict[str, str]) -> None:
    THREAD_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREAD_FILE.write_text(json.dumps(thread), encoding="utf-8")


def clear_thread() -> None:
    try:
        THREAD_FILE.unlink()
    except OSError:
        pass


def pending_question(snapshot) -> Optional[dict]:
    """The question the graph is waiting on (from a subgraph too), or None if it finished."""
    for task in snapshot.tasks or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return intr.value
    for intr in getattr(snapshot, "interrupts", ()) or ():
        return intr.value
    return None


# ---------------------------------------------------------------- chat


async def converse(app, thread: dict[str, str], step: Any) -> None:
    """Runs one request until it finishes, asking you whatever the graph asks.
    step=None continues a saved request at the question it stopped on."""
    config = {"configurable": {"thread_id": thread["thread_id"]}, "recursion_limit": 200}
    while True:
        if step is not None:
            async for _ in app.astream(step, config, stream_mode="updates"):
                pass
        snapshot = await app.aget_state(config)
        question = pending_question(snapshot)
        if question is None:
            print("\n" + (snapshot.values.get("result") or "Done."))
            return
        print("\n" + question.get("prompt", str(question)))
        step = Command(resume=await asyncio.to_thread(input, "> "))


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    services = Services(cfg, use_amazon=not a.no_amazon)
    db = resolve(cfg.checkpoint_db)
    db.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncExitStack() as stack:
        await services.connect(stack)
        stack.push_async_callback(services.close)
        if not services.food and not services.grocery:
            print("No app connected; nothing to do.")
            return
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(db)))
        app = build_main_graph(cfg, services.food, services.grocery,
                               ensure_grocery=services.ensure_grocery).compile(checkpointer=saver)

        print("\nShopping agent ready. Type what you want (help, quit).")

        unfinished = load_thread()
        if unfinished:
            config = {"configurable": {"thread_id": unfinished["thread_id"]}}
            try:
                waiting = pending_question(await app.aget_state(config))
            except Exception:  # noqa: BLE001
                waiting = None
            if waiting:
                answer = (await asyncio.to_thread(
                    input, f"\nYou didn't finish '{unfinished['message']}'. Continue it? (Y/n) ")).strip().lower()
                if answer not in ("n", "no"):
                    try:
                        await converse(app, unfinished, None)
                    except Exception as e:  # noqa: BLE001
                        print(f"\nThat request failed: {describe_error(e)}")
            clear_thread()

        while True:
            message = (await asyncio.to_thread(input, "\nYou: ")).strip()
            if not message:
                continue
            if message.lower() in QUIT:
                break
            if message.lower() in ("help", "?"):
                print(HELP)
                continue
            thread = {"thread_id": uuid.uuid4().hex, "message": message}
            save_thread(thread)
            try:
                await converse(app, thread, {"user_message": message})
            except Exception as e:  # noqa: BLE001  one failed request doesn't end the chat
                print(f"\nThat request failed: {describe_error(e)}")
            clear_thread()
    print("Bye.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--no-amazon", action="store_true", help="don't open the browser (Zepto only for groceries)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        print("\nStopped. If you were in the middle of a request, run again to continue it.")
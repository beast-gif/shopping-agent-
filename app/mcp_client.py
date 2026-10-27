"""Connects to the official MCP servers (Zepto, Swiggy, Zomato) with OAuth + PKCE,
caches tokens on disk, and enforces the tool filter.

Tool filter (this is what enforces "stop at the cart"):
  1. A tool whose name matches any blocked_tool_patterns is never callable.
  2. If allowed_tools[server] is non-empty, only those tools are callable.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import webbrowser
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.types import CallToolResult, PaginatedRequestParams, Tool
from pydantic import AnyUrl

from app.approval import ApprovalError, OrderApproval
from app.config import AppConfig, resolve


class BlockedToolError(PermissionError):
    pass


def describe_error(e: BaseException) -> str:
    """Readable message, unwrapping anyio/asyncio ExceptionGroups to the real cause."""
    if isinstance(e, BaseExceptionGroup):
        return " | ".join(describe_error(sub) for sub in e.exceptions)
    msg = f"{type(e).__name__}: {e}"
    response = getattr(e, "response", None)
    if response is not None:
        try:
            msg += f" [HTTP {response.status_code} {response.request.url}] {response.text[:300]}"
        except Exception:  # noqa: BLE001
            pass
    return msg


class McpToolError(RuntimeError):
    pass


# ---------------------------------------------------------------- order rules
#
# The complete list of order/payment tools the agent can EVER call, and the argument
# rules each call must pass. config.yaml (ordering.tools) can switch these off but can't
# add to them: a tool missing here is refused even if listed there.
#   needs_approval(args) -> True if this call creates a real order (needs 'confirm <total>')
#   check(args)          -> None if allowed, else the reason it's refused


def _upi_only_swiggy(a: dict) -> Optional[str]:
    if str(a.get("paymentMethod", "")).strip().upper() != "UPI":
        return "Swiggy orders must use paymentMethod='UPI' (cash on delivery is never allowed)"
    return None


def _zepto_online(a: dict) -> Optional[str]:
    if a.get("useZeptoCash"):
        return "Zepto Cash (wallet) payments are not allowed"
    if a.get("riderTip") not in (None, 0, 0.0):
        return "Rider tips are not added automatically"
    return None


def _zomato_upi(a: dict) -> Optional[str]:
    if a.get("payment_method_type") != "upi":
        return "Zomato checkout must use payment_method_type='upi' (cash on delivery is never allowed)"
    return None


def _never(_: dict) -> bool:
    return False


ORDER_RULES: dict[tuple[str, str], tuple[Any, Any]] = {
    # (server, tool): (needs_approval, check)
    ("zepto", "get_payment_methods"): (_never, None),
    ("zepto", "create_online_payment_order"): (lambda a: bool(a.get("confirmOrder")), _zepto_online),
    ("zepto", "check_payment_status"): (_never, lambda a: "poll=true is not used" if a.get("poll") else None),
    ("swiggy_food", "get_payment_options"): (_never, None),
    ("swiggy_food", "place_food_order"): (lambda a: True, _upi_only_swiggy),
    ("swiggy_food", "check_payment_status"): (_never, lambda a: "finalize is widget-only" if a.get("finalize") else None),
    ("swiggy_food", "confirm_order"): (_never, None),
    ("zomato", "checkout_cart"): (lambda a: True, _zomato_upi),
    ("zomato", "get_order_tracking_info"): (_never, None),
}


# ---------------------------------------------------------------- token storage

class FileTokenStorage(TokenStorage):
    """Stores OAuth tokens + registered client info in .tokens/<server>.json"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def get_tokens(self) -> Optional[OAuthToken]:
        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        data = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)


# ---------------------------------------------------------------- OAuth callback

class _OAuthCallback:
    """Tiny localhost server that catches ?code=...&state=...&iss=... after login.
    Started in the redirect handler (before the browser opens) so a fast
    redirect can't arrive before the server is listening."""

    def __init__(self, port: int):
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._future: Optional[asyncio.Future] = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._future = future

        def deliver(query: dict[str, list[str]]) -> None:
            if future.done():
                return
            code = query.get("code", [None])[0]
            if code:
                future.set_result(AuthorizationCodeResult(
                    code=code,
                    state=query.get("state", [None])[0],
                    iss=query.get("iss", [None])[0],      # RFC 9207: Zepto sends and requires this
                ))
            else:
                error = query.get("error_description", query.get("error", ["no code returned"]))[0]
                future.set_exception(RuntimeError(f"OAuth login failed: {error}"))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/callback":            # e.g. the browser asking for /favicon.ico
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                ok = "code" in query
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = "Login received. You can close this tab." if ok else "Login failed. Check the terminal."
                self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())
                loop.call_soon_threadsafe(deliver, query)

            def log_message(self, *args):  # silence default logging
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    async def wait(self, timeout: float = 300) -> AuthorizationCodeResult:
        if self._future is None:
            raise RuntimeError("OAuth callback server was never started")
        try:
            return await asyncio.wait_for(self._future, timeout)
        finally:
            if self._server:
                self._server.shutdown()
                self._server.server_close()
                self._server = None


# ---------------------------------------------------------------- result parsing

def parse_result(result: Any) -> Any:
    """CallToolResult -> python object. Prefers structured_content, then JSON text.
    Unwraps Swiggy-style {success, data} envelopes."""
    if not isinstance(result, CallToolResult):
        raise McpToolError(f"Server asked for extra input or returned {type(result).__name__}; not supported")

    text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    if result.is_error:
        raise McpToolError(text or "MCP tool returned an error")

    payload: Any = result.structured_content
    if payload is None:
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            return text

    if isinstance(payload, dict) and "success" in payload and "data" in payload:
        if not payload.get("success"):
            raise McpToolError(json.dumps(payload.get("error") or payload)[:500])
        return payload["data"]
    return payload


# ---------------------------------------------------------------- hub

class McpHub:
    """One object holding a live session per MCP server.

        async with McpHub(cfg) as hub:
            await hub.connect_all()
            data = await hub.call("zepto", "search_products", {"query": "ice cream"})

    Open and close the hub in the same asyncio task (the MCP transport requires it).
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, dict[str, Tool]] = {}
        self._blocked = [re.compile(p, re.IGNORECASE) for p in cfg.mcp.blocked_tool_patterns]

    async def __aenter__(self) -> "McpHub":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- auth

    def _oauth(self, name: str, url: str) -> OAuthClientProvider:
        port = self.cfg.mcp.oauth_redirect_port
        callback = _OAuthCallback(port)

        async def on_redirect(authorization_url: str) -> None:
            callback.start()
            print(f"\n[{name}] Opening browser to log in. If it doesn't open, visit:\n{authorization_url}\n")
            webbrowser.open(authorization_url)

        async def on_callback() -> AuthorizationCodeResult:
            return await callback.wait()

        return OAuthClientProvider(
            server_url=url,
            client_metadata=OAuthClientMetadata(
                client_name="AR7 Shopping Agent",
                redirect_uris=[AnyUrl(f"http://localhost:{port}/callback")],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=FileTokenStorage(resolve(self.cfg.mcp.token_dir) / f"{name}.json"),
            redirect_handler=on_redirect,
            callback_handler=on_callback,
        )

    # ---- connect

    async def connect(self, name: str) -> None:
        if name in self.sessions:
            return
        server = self.cfg.mcp.servers[name]
        server_stack = AsyncExitStack()
        try:
            http = await server_stack.enter_async_context(
                httpx2.AsyncClient(
                    auth=self._oauth(name, server.url),
                    timeout=httpx2.Timeout(30.0, read=300.0),
                )
            )
            read, write = await server_stack.enter_async_context(
                streamable_http_client(server.url, http_client=http)
            )
            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = await self._list_all_tools(session)
        except BaseException:
            await server_stack.aclose()
            raise
        self._stack.push_async_callback(server_stack.aclose)
        self.sessions[name] = session
        self._tools[name] = {t.name: t for t in tools}

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> list[Tool]:
        """list_tools is paginated; follow next_cursor until done."""
        tools: list[Tool] = []
        cursor: Optional[str] = None
        while True:
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def connect_all(self, names: Optional[list[str]] = None) -> dict[str, str]:
        """Connects one server at a time (they share the OAuth callback port).
        Returns {server: error message} for any that failed; the rest stay usable."""
        names = names or [n for n, s in self.cfg.mcp.servers.items() if s.enabled]
        errors: dict[str, str] = {}
        for name in names:
            try:
                await self.connect(name)
            except Exception as e:  # noqa: BLE001
                errors[name] = describe_error(e)
        return errors

    # ---- tool filter

    def is_allowed(self, server: str, tool_name: str) -> bool:
        if any(p.search(tool_name) for p in self._blocked):
            return False
        allow = self.cfg.mcp.allowed_tools.get(server) or []
        return not allow or tool_name in allow

    def all_tools(self, server: str) -> list[Tool]:
        """Every tool the server exposes, blocked ones included (for inspection only)."""
        return list(self._tools.get(server, {}).values())

    def tools(self, server: str) -> list[Tool]:
        """Only the tools the workflow may call."""
        return [t for t in self.all_tools(server) if self.is_allowed(server, t.name)]

    # ---- call

    async def call(self, server: str, tool: str, args: Optional[dict[str, Any]] = None) -> Any:
        if not self.is_allowed(server, tool):
            raise BlockedToolError(f"{server}.{tool} is blocked by config (stop-at-cart rule)")
        if server not in self.sessions:
            raise McpToolError(f"{server} is not connected")
        if tool not in self._tools[server]:
            raise McpToolError(f"{server} has no tool named {tool!r}")
        result = await self.sessions[server].call_tool(tool, args or {})
        return parse_result(result)

    async def call_order(self, server: str, tool: str, args: Optional[dict[str, Any]] = None,
                         approval: Optional[OrderApproval] = None) -> Any:
        """The only path to order/payment tools. Checks, in order:
        ordering enabled -> tool in ORDER_RULES -> tool in config ordering.tools ->
        argument rules (UPI only, no wallet) -> a valid 'confirm <total>' approval
        for calls that create an order. The approval is used up by the call."""
        args = dict(args or {})
        ordering = self.cfg.ordering
        if not ordering.enabled:
            raise BlockedToolError("Ordering is disabled (config.yaml ordering.enabled)")
        rule = ORDER_RULES.get((server, tool))
        if rule is None:
            raise BlockedToolError(f"{server}.{tool} is not an allowed order tool")
        if tool not in (ordering.tools.get(server) or []):
            raise BlockedToolError(f"{server}.{tool} is not enabled in config.yaml ordering.tools")
        needs_approval, check = rule
        if check is not None:
            reason = check(args)
            if reason:
                raise BlockedToolError(f"{server}.{tool} refused: {reason}")
        if needs_approval(args):
            if approval is None:
                raise ApprovalError(f"{server}.{tool} places an order and needs your typed confirmation")
            approval.check(self.cfg, server)
            approval.used = True            # spent before the call, so a retry can't reuse it
        if server not in self.sessions:
            raise McpToolError(f"{server} is not connected")
        if tool not in self._tools[server]:
            raise McpToolError(f"{server} has no tool named {tool!r}")
        result = await self.sessions[server].call_tool(tool, args)
        return parse_result(result)

    async def close(self) -> None:
        await self._stack.aclose()
        self.sessions.clear()
        self._tools.clear()
"""Logs in to each MCP server and lists its tools, marking which are blocked.

Run from the project root:
    python scripts/list_tools.py              # all enabled servers
    python scripts/list_tools.py zepto        # just one

Writes tools_dump.json (full tool schemas). Send that file back so the
adapters in Phase 2 use the real tool names and arguments.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.mcp_client import McpHub  # noqa: E402


async def main(names: list[str]) -> None:
    cfg = load_config()
    names = names or [n for n, s in cfg.mcp.servers.items() if s.enabled]
    dump: dict[str, object] = {}

    async with McpHub(cfg) as hub:
        errors = await hub.connect_all(names)
        for name in names:
            print(f"\n=== {name} ===")
            if name in errors:
                print(f"  CONNECT FAILED: {errors[name]}")
                dump[name] = {"error": errors[name]}
                continue

            tools = sorted(hub.all_tools(name), key=lambda t: t.name)
            for tool in tools:
                schema = tool.input_schema or {}
                props = list(schema.get("properties", {}).keys())
                required = set(schema.get("required", []))
                args = ", ".join(f"{p}*" if p in required else p for p in props)
                tag = "ok     " if hub.is_allowed(name, tool.name) else "BLOCKED"
                print(f"  [{tag}] {tool.name}({args})")
                if tool.description:
                    print(f"            {tool.description.strip().splitlines()[0][:110]}")
            dump[name] = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools]

    out = ROOT / "tools_dump.json"
    out.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n* = required argument.  Full schemas written to {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
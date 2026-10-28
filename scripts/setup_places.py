"""One-time setup: groups your saved addresses from Zepto, Swiggy and Zomato into places
(home, college ...), lets you fix the grouping, adds Wi-Fi names, and saves places.yaml.

Run from the project root (re-run any time you add an address in an app):
    python scripts/setup_places.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters import SwiggyAdapter, ZeptoAdapter, ZomatoAdapter  # noqa: E402
from app.config import load_config  # noqa: E402
from app.location import current_ssid  # noqa: E402
from app.mcp_client import McpHub, describe_error  # noqa: E402
from app.places import (  # noqa: E402
    PlaceBook,
    default_name,
    load_places,
    place_from_group,
    save_places,
    suggest_groups,
)
from app.schemas import SavedAddress  # noqa: E402

APPS = [("zepto", "zepto", "ZP", ZeptoAdapter), ("swiggy_food", "swiggy", "SW", SwiggyAdapter),
        ("zomato", "zomato", "ZM", ZomatoAdapter)]


def parse_custom(text: str, by_code: dict[str, SavedAddress]) -> list[tuple[str, list[SavedAddress]]]:
    """'home=ZP1,SW1,ZM4; college=ZP3,SW3' -> [('home', [...]), ('college', [...])]"""
    groups, used = [], set()
    for part in text.split(";"):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"'{part.strip()}' needs a name, like home=ZP1,SW1")
        name, codes = part.split("=", 1)
        name = name.strip().lower()
        members = []
        for code in (c.strip().upper() for c in codes.split(",") if c.strip()):
            if code not in by_code:
                raise ValueError(f"unknown code {code}")
            if code in used:
                raise ValueError(f"{code} is used twice")
            used.add(code)
            members.append(by_code[code])
        apps = [m.platform for m in members]
        if len(apps) != len(set(apps)):
            raise ValueError(f"'{name}' has two addresses from the same app")
        if not name or not members:
            raise ValueError(f"'{part.strip()}' is empty")
        groups.append((name, members))
    return groups


async def main() -> None:
    cfg = load_config()
    old = load_places(cfg)

    addresses: list[SavedAddress] = []
    code_of: dict[int, str] = {}
    by_code: dict[str, SavedAddress] = {}

    async with McpHub(cfg) as hub:
        errors = await hub.connect_all([s for s, *_ in APPS])
        for server, app, prefix, Adapter in APPS:
            if server in errors:
                print(f"[{app}] not connected: {errors[server]}")
                continue
            try:
                found = await Adapter(hub).list_addresses()
            except Exception as e:  # noqa: BLE001
                print(f"[{app}] couldn't list addresses: {describe_error(e)}")
                continue
            print(f"\n{app.title()}")
            for i, a in enumerate(found, 1):
                code = f"{prefix}{i}"
                code_of[id(a)] = code
                by_code[code] = a
                addresses.append(a)
                coords = f"  ({a.lat:.5f}, {a.lng:.5f})" if a.lat is not None and a.lng is not None else ""
                print(f"  {code:5} {(a.label or '-')[:8]:8} {a.text[:80]}{coords}")

    if not addresses:
        print("No addresses found; nothing to set up.")
        return

    groups = suggest_groups(addresses, cfg.locations.group_threshold)
    taken: set[str] = set()
    named: list[tuple[str, list[SavedAddress]]] = []
    print("\nSuggested places:")
    for g in groups:
        name = default_name(g, taken)
        taken.add(name)
        named.append((name, g))
        print(f"  {name:10} = {', '.join(code_of[id(a)] for a in g)}")

    while True:
        text = input("\nEnter to accept, or type your own, e.g.  home=ZP1,SW1,ZM4; college=ZP3,SW3,ZM3\n> ").strip()
        if not text:
            break
        try:
            named = parse_custom(text, by_code)
            listed = {id(a) for _, g in named for a in g}
            leftovers = [a for a in addresses if id(a) not in listed]
            if leftovers:
                print("  Not in any place (they won't be used): "
                      + ", ".join(code_of[id(a)] for a in leftovers))
            break
        except ValueError as e:
            print(f"  {e}. Try again.")

    ssid = current_ssid()
    if ssid:
        print(f"\nYou're on Wi-Fi '{ssid}' right now.")
    book = PlaceBook()
    for name, group in named:
        answer = input(f"\nName for {name!r} = {', '.join(code_of[id(a)] for a in group)}"
                       f"  (Enter keeps '{name}'; add other words after commas, e.g. college, clg, hostel): ").strip()
        words = [w.strip().lower() for w in answer.split(",") if w.strip()] if answer else [name]
        place = place_from_group(words[0], group)
        place.aliases = words[1:]

        previous = old.get(place.name)
        hint = f"'here' = {ssid}; " if ssid else ""
        wifi = input(f"  Wi-Fi names at {place.name} ({hint}comma-separated; Enter to "
                     f"{'keep ' + str(previous.wifi) if previous and previous.wifi else 'skip'}): ").strip()
        if wifi:
            place.wifi = [ssid if w.strip().lower() == "here" and ssid else w.strip()
                          for w in wifi.split(",") if w.strip()]
        elif previous:
            place.wifi = previous.wifi
        book.places.append(place)

    path = save_places(cfg, book)
    print(f"\nSaved {len(book.places)} places to {path}")
    for p in book.places:
        apps = ", ".join(sorted(p.addresses))
        gps = "has coordinates" if p.lat is not None else "no coordinates (needs Wi-Fi or you saying it)"
        print(f"  {p.name:10} apps: {apps:22} wifi: {p.wifi or '-'}   {gps}")


if __name__ == "__main__":
    asyncio.run(main())
"""Shows which place the agent would deliver to right now, and why.

Run from the project root:
    python scripts/where_am_i.py
    python scripts/where_am_i.py --say college        # as if you'd typed "I'm at college"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.location import (  # noqa: E402
    current_ssid,
    detect_place,
    haversine_m,
    remember_place,
    windows_fix,
)
from app.places import load_places, places_path  # noqa: E402


async def main(a: argparse.Namespace) -> None:
    cfg = load_config()
    book = load_places(cfg)
    if not book.places:
        print(f"No places yet ({places_path(cfg)} missing). Run: python scripts/setup_places.py")
        return

    print(f"Wi-Fi: {current_ssid() or '(not connected / unknown)'}")
    fix = await windows_fix(cfg.locations.gps_timeout_s)
    if fix:
        print(f"Windows location: ({fix.lat:.5f}, {fix.lng:.5f}) ±{fix.accuracy_m:.0f} m via {fix.source or '?'}")
        for p in book.places:
            if p.lat is not None and p.lng is not None:
                print(f"   {haversine_m(fix.lat, fix.lng, p.lat, p.lng):8.0f} m  to {p.name}")
    else:
        print("Windows location: not available (allow it in Settings > Privacy & security > Location)")

    found = await detect_place(book, cfg, a.say)
    if not found.place:
        print(f"\nPlace: unknown ({found.how}). The agent would ask you.")
        return
    p = found.place
    print(f"\nPlace: {p.name}   (decided by: {found.how})")
    for app in ("zepto", "swiggy", "zomato"):
        text = p.texts.get(app)
        print(f"   {app:7} {text[:75] if text else '- no saved address here, this app is skipped'}")
    if a.remember:
        remember_place(p)
        print("Remembered as last used.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--say", help="pretend you said this place, e.g. college")
    parser.add_argument("--remember", action="store_true", help="save the result as 'last used'")
    asyncio.run(main(parser.parse_args()))
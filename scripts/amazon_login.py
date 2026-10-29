"""One-time: log in to Amazon in the agent's own browser profile.

Run from the project root:
    python scripts/amazon_login.py

A Chrome window opens on amazon.in. Sign in (phone/email + OTP), then come back here and
press Enter. The login is kept in .browser/amazon_now (git-ignored), so you won't need to
do this again until Amazon logs you out.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.browser import amazon_browser, signed_in_name  # noqa: E402
from app.config import load_config  # noqa: E402


async def main() -> None:
    cfg = load_config()
    async with amazon_browser(cfg, headless=False) as context:
        name = await signed_in_name(context, cfg.amazon_now.base_url)
        if name:
            print(f"Already signed in as {name}.")
        else:
            print("Sign in to Amazon in the Chrome window that just opened.")
        await asyncio.to_thread(input, "Press Enter here once you're signed in (or to close)... ")
        name = await signed_in_name(context, cfg.amazon_now.base_url)
        print(f"Signed in as {name}. Login saved." if name else "Still not signed in; run this again.")


if __name__ == "__main__":
    asyncio.run(main())

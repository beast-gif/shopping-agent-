"""Opens the agent's own browser profile (.browser/...), where you log in to Amazon once.
The profile keeps the login, so later runs are already signed in.

It's a separate profile from your everyday browser, so the two never interfere; your normal
Brave/Chrome can stay open while the agent runs.

channel in config.yaml:
  brave    -> your installed Brave (found automatically, or set amazon_now.executable_path)
  chrome   -> your installed Google Chrome
  chromium -> Playwright's own Chromium (python -m playwright install chromium)
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import BrowserContext, async_playwright

from app.config import AppConfig, resolve

# Makes the window look like an ordinary browser rather than a test browser.
_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]


def find_brave(explicit: str = "") -> Optional[str]:
    """brave.exe from config, else the usual Windows/macOS/Linux install locations."""
    candidates = [explicit] if explicit else []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(str(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"))
    candidates += ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                   "/usr/bin/brave-browser", "/usr/bin/brave"]
    return next((c for c in candidates if c and Path(c).exists()), None)


@asynccontextmanager
async def amazon_browser(cfg: AppConfig, headless: bool | None = None) -> AsyncIterator[BrowserContext]:
    amz = cfg.amazon_now
    profile = resolve(amz.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        options = dict(
            headless=amz.headless if headless is None else headless,
            args=_STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1366, "height": 860},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        if amz.channel == "brave":
            exe = find_brave(amz.executable_path)
            if not exe:
                raise RuntimeError("Brave not found. Set amazon_now.executable_path in config.yaml to brave.exe "
                                   r"(e.g. C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe)")
            context = await pw.chromium.launch_persistent_context(str(profile), executable_path=exe, **options)
        else:
            try:
                context = await pw.chromium.launch_persistent_context(str(profile), channel=amz.channel, **options)
            except Exception as e:  # noqa: BLE001  Chrome not installed -> Playwright's Chromium
                if amz.channel in ("chromium", ""):
                    raise
                print(f"(Couldn't start {amz.channel}: {type(e).__name__}. Using Playwright's Chromium; "
                      "if that fails too, run: python -m playwright install chromium)")
                context = await pw.chromium.launch_persistent_context(str(profile), **options)
        try:
            yield context
        finally:
            await context.close()


async def signed_in_name(context: BrowserContext, base_url: str) -> str | None:
    """'Amrith' if Amazon shows 'Hello, Amrith' in the header, else None."""
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(base_url, wait_until="domcontentloaded")
    text = await page.evaluate(
        "() => (document.querySelector('#nav-link-accountList-nav-line-1') || {}).innerText || ''")
    text = (text or "").strip()
    if not text or "sign in" in text.lower():
        return None
    return text.replace("Hello,", "").strip() or text
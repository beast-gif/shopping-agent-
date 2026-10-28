"""Works out which of your places you're at, in this order:

  1. what you said ("I'm at college")            -> exact
  2. the Wi-Fi you're connected to                -> exact, if you mapped that Wi-Fi
  3. Windows location (Wi-Fi/GPS positioning)     -> nearest place, if the fix is precise enough
  4. the place you used last time
  5. nothing -> ask you

GPS rule (theory). Haversine distance between two points:
    d = 2R · asin( sqrt( sin²(Δφ/2) + cos φ1 · cos φ2 · sin²(Δλ/2) ) ),  R = 6,371,000 m
A fix with accuracy ±a metres picks the nearest place P1 only if
    d1 ≤ match_radius   and   d1 + a < d2        (d2 = distance to the second-nearest place)
i.e. even the worst case inside the error circle is still closer to P1. Your home and family
flats are ~150 m apart, so GPS alone often can't tell them apart; the Wi-Fi name can.
"""
from __future__ import annotations

import asyncio
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from app.config import AppConfig, resolve
from app.places import Place, PlaceBook, match_place_name

EARTH_RADIUS_M = 6_371_000


@dataclass
class Fix:
    lat: float
    lng: float
    accuracy_m: float
    source: str            # 'wifi', 'satellite', 'cellular', 'ip', ...


@dataclass
class Detected:
    place: Optional[Place]
    how: str               # shown to you: "Wi-Fi 'Home_5G'", "GPS ±35 m", "last used", ...


# ---------------------------------------------------------------- distance


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- Wi-Fi


def current_ssid() -> Optional[str]:
    """Name of the Wi-Fi network you're connected to, or None."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                 capture_output=True, text=True, timeout=5).stdout
            # 'SSID : name' (but not 'BSSID : ...'); the lookbehind skips BSSID
            m = re.search(r"^\s*(?<!B)SSID\s*:\s*(.+?)\s*$", out, flags=re.MULTILINE)
            return m.group(1) if m else None
        if sys.platform == "darwin":
            out = subprocess.run(["networksetup", "-getairportnetwork", "en0"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
            return m.group(1).strip() if m else None
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1] or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


# ---------------------------------------------------------------- Windows location


async def windows_fix(timeout_s: float = 10) -> Optional[Fix]:
    """Location from Windows (Settings > Privacy & security > Location must allow desktop apps).
    None if unavailable, denied, or it takes longer than timeout_s."""
    if sys.platform != "win32":
        return None
    try:
        import winrt.windows.foundation  # noqa: F401  makes WinRT async calls awaitable
        from winrt.windows.devices.geolocation import Geolocator
    except ImportError:
        return None
    try:
        pos = await asyncio.wait_for(Geolocator().get_geoposition_async(), timeout_s)
    except Exception:  # noqa: BLE001  access denied, location off, timeout ...
        return None
    coord = pos.coordinate
    point = coord.point.position
    return Fix(lat=point.latitude, lng=point.longitude, accuracy_m=float(coord.accuracy),
               source=_source_name(getattr(coord, "position_source", None)))


# Windows PositionSource values (your run printed 'via 2' = Wi-Fi)
_POSITION_SOURCES = {0: "cellular", 1: "satellite", 2: "wifi", 3: "ip_address",
                     4: "unknown", 5: "default", 6: "obfuscated"}


def _source_name(value) -> str:
    """The enum can come back as a number (2) or a name ('PositionSource.WI_FI')."""
    try:
        return _POSITION_SOURCES.get(int(value), "unknown")
    except (TypeError, ValueError):
        name = str(value or "").split(".")[-1].lower().replace("_", "")
        return {"wifi": "wifi", "ipaddress": "ip_address"}.get(name, name or "unknown")


def place_from_fix(book: PlaceBook, fix: Fix, cfg: AppConfig) -> tuple[Optional[Place], str]:
    loc = cfg.locations
    if fix.source in ("ip_address", "obfuscated"):
        return None, f"location came from {fix.source.replace('_', ' ')} (±{fix.accuracy_m:.0f} m), too vague"
    if fix.accuracy_m > loc.max_gps_accuracy_m:
        return None, f"location too vague (±{fix.accuracy_m:.0f} m)"
    ranked = sorted(
        ((haversine_m(fix.lat, fix.lng, p.lat, p.lng), p) for p in book.places if p.lat is not None and p.lng is not None),
        key=lambda x: x[0],
    )
    if not ranked:
        return None, "no place has coordinates"
    d1, best = ranked[0]
    if d1 > loc.match_radius_m:
        return None, f"nearest place '{best.name}' is {d1:.0f} m away"
    if len(ranked) > 1:
        d2, second = ranked[1]
        if d1 + fix.accuracy_m >= d2:
            return None, (f"can't tell '{best.name}' ({d1:.0f} m) from '{second.name}' ({d2:.0f} m) "
                          f"at ±{fix.accuracy_m:.0f} m")
    return best, f"GPS ±{fix.accuracy_m:.0f} m, {d1:.0f} m from '{best.name}'"


# ---------------------------------------------------------------- last used


def _last_path():
    return resolve(".state/last_place.txt")


def remember_place(place: Place) -> None:
    path = _last_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(place.name, encoding="utf-8")


def last_place(book: PlaceBook) -> Optional[Place]:
    path = _last_path()
    return book.get(path.read_text(encoding="utf-8").strip()) if path.exists() else None


# ---------------------------------------------------------------- the whole chain


async def detect_place(book: PlaceBook, cfg: AppConfig, hint: Optional[str] = None) -> Detected:
    notes: list[str] = []

    said = match_place_name(book, hint)
    if said:
        return Detected(said, f"you said '{hint}'")
    if hint:
        # You named a place we don't know: guessing from Wi-Fi/GPS would overrule what you
        # said, so return no place and let the agent ask.
        known = ", ".join(p.name for p in book.places)
        return Detected(None, f"no place called '{hint}' (yours: {known})")

    if cfg.locations.auto_detect:
        ssid = current_ssid()
        if ssid:
            for p in book.places:
                if ssid in p.wifi:
                    return Detected(p, f"Wi-Fi '{ssid}'")
            notes.append(f"Wi-Fi '{ssid}' isn't mapped to a place")

        fix = await windows_fix(cfg.locations.gps_timeout_s)
        if fix:
            place, why = place_from_fix(book, fix, cfg)
            if place:
                return Detected(place, why)
            notes.append(why)
        else:
            notes.append("no Windows location")

    last = last_place(book)
    if last:
        return Detected(last, "last used" + (f" ({'; '.join(notes)})" if notes else ""))
    return Detected(None, "; ".join(notes) or "no places set up")
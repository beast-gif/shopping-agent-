"""Saves raw app responses to probes/ (git-ignored) so the parsing can be checked against them."""
from __future__ import annotations

import json
from typing import Any

from app.config import resolve


def save_debug(name: str, data: Any) -> None:
    try:
        path = resolve(f"probes/{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError:
        pass        # debugging aid only; never break a run over it
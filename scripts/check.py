"""Finds which Velona base URL + model ID actually works with the OpenAI chat format.

Run from the project root:
    python scripts/check_velona.py

Prints the status and the first part of each response. Never prints your API key.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

key = os.environ.get("VELONA_API_KEY", "").strip()
base = os.environ.get("VELONA_BASE_URL", "").strip().rstrip("/")
model = os.environ.get("VELONA_MODEL", "").strip()

if not key:
    sys.exit("VELONA_API_KEY is empty in .env")

print(f"Your .env -> base_url={base!r}  model={model!r}\n")

bases = [b for b in dict.fromkeys([
    base,
    "https://velona.in/gateway/v1",
    "https://velona.in/api/v1",
    "https://velona.in/v1",
    "https://api.velona.in/v1",
]) if b]

models = [m for m in dict.fromkeys([model, "openai/gpt-4o-mini", "gpt-4o-mini"]) if m]
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def show(label: str, r: httpx.Response) -> None:
    body = r.text.replace("\n", " ")[:200]
    print(f"  {label:<38} -> {r.status_code}  {body}")


with httpx.Client(timeout=30, follow_redirects=True) as client:
    for b in bases:
        print(f"BASE {b}")
        try:
            show("GET  /models", client.get(f"{b}/models", headers=headers))
            for m in models:
                r = client.post(
                    f"{b}/chat/completions",
                    headers=headers,
                    json={"model": m, "messages": [{"role": "user", "content": "say ok"}], "max_tokens": 5},
                )
                show(f"POST /chat/completions  model={m}", r)
                if r.status_code == 200:
                    print(f"\nWORKS ->  VELONA_BASE_URL={b}   VELONA_MODEL={m}")
                    sys.exit(0)
        except httpx.HTTPError as e:
            print(f"  connection failed: {type(e).__name__}: {e}")
        print()

print("No combination worked. Paste this whole output back.")
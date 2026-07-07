#!/usr/bin/env python3
"""
Diagnose why a given Anthropic model can / can't be used by this project.

It does two things with the SAME key + SDK path the gate uses:
  1) lists the models this API key can actually see (client.models.list)
  2) makes a minimal 1-token call to each candidate model and prints the exact
     outcome (OK, or the precise error: 404 not_found / 403 permission / auth / etc.)

Usage (from repo root):
    .venv\\Scripts\\python.exe scripts\\check_anthropic_models.py
    .venv\\Scripts\\python.exe scripts\\check_anthropic_models.py claude-opus-4-1 claude-sonnet-4-5
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import os

# Frontier candidates to probe by default (plus the known-good control). Edit/extend freely.
DEFAULT_CANDIDATES = [
    "claude-haiku-4-5-20251001",  # control: this is what the gate uses today
    "claude-sonnet-4-5",
    "claude-opus-4-1",
    "claude-opus-4-5",
]


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY not set (.env not loaded?)")
        return 1
    print(f"Using ANTHROPIC_API_KEY ...{api_key[-6:]}\n")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    print("=== Models this key can list ===")
    try:
        listed = list(client.models.list(limit=100))
        for m in listed:
            print(f"  {m.id}")
        if not listed:
            print("  (none returned)")
    except Exception as exc:
        print(f"  models.list failed: {type(exc).__name__}: {exc}")
    print()

    candidates = sys.argv[1:] or DEFAULT_CANDIDATES
    print("=== Minimal call per candidate model ===")
    for model in candidates:
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            print(f"  [OK ]  {model}  (stop={resp.stop_reason})")
        except Exception as exc:
            status = getattr(exc, "status_code", "")
            etype = getattr(getattr(exc, "body", None), "get", lambda *_: None)("error") if hasattr(exc, "body") else None
            print(f"  [ERR]  {model}  {type(exc).__name__}"
                  + (f" status={status}" if status else "")
                  + f": {str(exc)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

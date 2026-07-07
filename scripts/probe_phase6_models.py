#!/usr/bin/env python3
"""Probe the five Phase 6 roster model IDs (minimal 1-request each)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

ROSTER = [
    ("anthropic", "claude-opus-4-8"),
    ("openai", "gpt-5.5"),
    ("anthropic", "claude-haiku-4-5-20251001"),
    ("openai", "gpt-4.1"),
    ("gemini", "gemini-2.5-pro"),
]


def probe(vendor: str, model: str) -> str:
    try:
        if vendor == "anthropic":
            import anthropic

            anthropic.Anthropic().messages.create(
                model=model, max_tokens=8, messages=[{"role": "user", "content": "ping"}]
            )
        elif vendor == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
            }
            if model.lower().startswith("gpt-5"):
                kwargs["max_completion_tokens"] = 16
            else:
                kwargs["max_tokens"] = 8
            client.chat.completions.create(**kwargs)
        elif vendor == "gemini":
            from google import genai
            from google.genai import types

            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
            genai.Client(api_key=key).models.generate_content(
                model=model,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=8),
            )
        else:
            return "unknown vendor"
        return "OK"
    except Exception as exc:
        err = str(exc)
        if "404" in err or "not_found" in err.lower():
            return "404"
        if "401" in err:
            return "401"
        if "403" in err:
            return "403"
        return err[:160]


def main() -> int:
    ok = 0
    for vendor, model in ROSTER:
        status = probe(vendor, model)
        mark = "OK" if status == "OK" else "FAIL"
        print(f"  [{mark}] {vendor:10s} {model:32s} {status}")
        if status == "OK":
            ok += 1
    print(f"\n{ok}/{len(ROSTER)} models resolve")
    return 0 if ok == len(ROSTER) else 1


if __name__ == "__main__":
    sys.exit(main())

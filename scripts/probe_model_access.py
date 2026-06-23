#!/usr/bin/env python3
"""
Probe LLM API access across vendors configured in .env.

Run after adding keys to discover which model IDs work on this account.
Does not spend more than one minimal request per model candidate.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

CANDIDATES: dict[str, list[str]] = {
    "anthropic": [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-20250514",
        "claude-sonnet-4-0",
        "claude-3-5-sonnet-latest",
        "claude-3-7-sonnet-latest",
        "claude-opus-4-20250514",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-1.5-flash-002",
        "gemini-1.5-pro-002",
    ],
}

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _has_key(vendor: str) -> bool:
    keys = ENV_KEYS[vendor]
    if isinstance(keys, str):
        return bool(os.environ.get(keys))
    return any(os.environ.get(k) for k in keys)


def _probe_anthropic(model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    client.messages.create(
        model=model, max_tokens=8, messages=[{"role": "user", "content": "ping"}]
    )
    return "OK"


def _probe_openai_compat(model: str, *, api_key: str, base_url: str | None) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    client.chat.completions.create(
        model=model,
        max_tokens=8,
        messages=[{"role": "user", "content": "ping"}],
    )
    return "OK"


def _probe_gemini(model: str, api_key: str) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)
    client.models.generate_content(model=model, contents="ping")
    return "OK"


def probe_vendor(vendor: str, models: list[str] | None = None) -> list[tuple[str, str]]:
    if not _has_key(vendor):
        return [(m, "SKIP (no API key)") for m in (models or CANDIDATES[vendor])]

    targets = models or CANDIDATES[vendor]
    results: list[tuple[str, str]] = []

    for model in targets:
        try:
            if vendor == "anthropic":
                status = _probe_anthropic(model)
            elif vendor == "openai":
                status = _probe_openai_compat(
                    model, api_key=os.environ["OPENAI_API_KEY"], base_url=None
                )
            elif vendor == "deepseek":
                status = _probe_openai_compat(
                    model,
                    api_key=os.environ["DEEPSEEK_API_KEY"],
                    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                )
            elif vendor == "gemini":
                key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
                status = _probe_gemini(model, key)
            else:
                status = "unknown vendor"
            results.append((model, status))
        except Exception as exc:
            err = str(exc)
            if "404" in err or "not_found" in err.lower():
                results.append((model, "404"))
            elif "401" in err or "authentication" in err.lower():
                results.append((model, "401 auth"))
            elif "403" in err:
                results.append((model, "403 forbidden"))
            else:
                results.append((model, err[:120]))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe model API access from .env keys")
    parser.add_argument(
        "--vendor",
        choices=["anthropic", "openai", "deepseek", "gemini", "all"],
        default="all",
    )
    args = parser.parse_args()

    vendors = (
        ["anthropic", "openai", "deepseek", "gemini"]
        if args.vendor == "all"
        else [args.vendor]
    )

    any_ok = False
    for vendor in vendors:
        print(f"\n=== {vendor} ===")
        if not _has_key(vendor):
            print("  (no API key in .env — skipped)")
            continue
        for model, status in probe_vendor(vendor):
            mark = "OK" if status == "OK" else "--"
            print(f"  [{mark}] {model}: {status}")
            if status == "OK":
                any_ok = True

    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())

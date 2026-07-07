#!/usr/bin/env python3
"""Minimal OpenAI API probe for gpt-4.1 — prints full success/error JSON."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _dump_exc(exc: BaseException) -> dict:
    out: dict = {"type": type(exc).__name__, "message": str(exc)}
    for attr in ("status_code", "request_id", "code", "param", "body", "response"):
        if hasattr(exc, attr):
            val = getattr(exc, attr)
            if attr == "response" and val is not None:
                try:
                    out["response_status"] = getattr(val, "status_code", None)
                    out["response_headers"] = dict(getattr(val, "headers", {}) or {})
                except Exception:
                    out["response"] = repr(val)
            else:
                out[attr] = val
    return out


def main() -> int:
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_TEST_MODEL", "gpt-4.1")
    base_url = os.environ.get("OPENAI_BASE_URL")

    print("=== OpenAI gpt-4.1 probe ===")
    print(f"model:     {model}")
    print(f"base_url:  {base_url or '(default)'}")
    print(f"key set:   {bool(api_key)} (len={len(api_key)})")
    print()

    if not api_key:
        print(json.dumps({"error": "OPENAI_API_KEY not set"}, indent=2))
        return 1

    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
            max_tokens=8,
        )
        payload = {
            "status": "ok",
            "id": resp.id,
            "model": resp.model,
            "finish_reason": resp.choices[0].finish_reason,
            "content": resp.choices[0].message.content,
            "usage": resp.usage.model_dump() if resp.usage else None,
        }
        print(json.dumps(payload, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", **_dump_exc(exc)}, indent=2, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

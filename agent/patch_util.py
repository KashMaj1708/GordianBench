"""Normalize model_patch text from LLM submissions before git apply."""

from __future__ import annotations

import re


def sanitize_model_patch(raw: str) -> str:
    """
    Strip markdown fences and common wrappers models add around diffs.

    Real models often wrap patches in ```diff ... ``` — git apply rejects that.
    """
    text = raw.strip()
    if not text:
        return text

    # ```diff\n...\n``` or ```\n...\n```
    fence = re.match(r"^```(?:\w+)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Single leading/trailing fence lines without full match
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # git apply requires a trailing newline on the patch file.
    if text and not text.endswith("\n"):
        text += "\n"
    return text

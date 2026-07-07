"""Normalize model_patch text from LLM submissions before git apply."""

from __future__ import annotations

import re


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _ensure_git_headers(text: str) -> str:
    """Insert diff --git lines when models omit them (common with Gemini)."""
    lines = text.splitlines()
    if not lines:
        return text

    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- a/"):
            prev = out[-1] if out else ""
            if not prev.startswith("diff --git"):
                old_path = line[len("--- a/") :]
                new_path = old_path
                if i + 1 < len(lines) and lines[i + 1].startswith("+++ b/"):
                    new_path = lines[i + 1][len("+++ b/") :]
                out.append(f"diff --git a/{old_path} b/{new_path}")
            out.append(line)
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


def _normalize_patch_structure(text: str) -> str:
    """Drop duplicate diff --git/index lines, then ensure one header per file section."""
    lines = [
        line
        for line in text.splitlines()
        if not line.startswith("diff --git") and not line.startswith("index ")
    ]
    return _ensure_git_headers("\n".join(lines))


def looks_truncated_patch(patch: str) -> bool:
    """True if any unified hunk declares more lines than its body contains."""
    if not patch.strip():
        return True

    lines = patch.splitlines()
    i = 0
    found_hunk = False

    while i < len(lines):
        line = lines[i]
        match = _HUNK_HEADER.search(line)
        if not match:
            i += 1
            continue

        found_hunk = True
        old_count = int(match.group(2) or 1)
        new_count = int(match.group(4) or 1)
        seen_old = 0
        seen_new = 0
        i += 1

        while i < len(lines) and (seen_old < old_count or seen_new < new_count):
            hline = lines[i]
            if hline.startswith("diff --git") or _HUNK_HEADER.search(hline):
                break
            if hline.startswith("\\"):
                i += 1
                continue
            if not hline:
                i += 1
                continue
            prefix = hline[0]
            if prefix == " ":
                seen_old += 1
                seen_new += 1
            elif prefix == "-":
                seen_old += 1
            elif prefix == "+":
                seen_new += 1
            else:
                return True
            i += 1

        if seen_old < old_count or seen_new < new_count:
            return True

    return not found_hunk and len(patch.strip()) < 80


def extract_patch_from_text(text: str) -> str:
    """Pull a unified diff from assistant prose (markdown fence or raw headers)."""
    if not text.strip():
        return ""
    fence = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return repair_model_patch(fence.group(1))
    if "--- a/" in text or "diff --git" in text:
        start = text.find("diff --git")
        if start == -1:
            start = text.find("--- a/")
        return repair_model_patch(text[start:])
    return ""


def repair_model_patch(raw: str) -> str:
    """Fence strip, line-ending normalize, git header repair when needed.

    File sections are counted by `--- ` lines (which covers both `--- a/<path>`
    and the `--- /dev/null` of a new file), NOT by `--- a/` alone. Counting only
    `--- a/` under-counts whenever a diff adds a new file, which used to trip the
    dedup path and strip the new file's `diff --git` header — producing a
    "corrupt patch" from otherwise-valid `git diff` output. Real git diffs now
    pass through untouched.
    """
    text = sanitize_model_patch(raw)
    if not text:
        return text
    file_sections = sum(1 for ln in text.splitlines() if ln.startswith("--- "))
    git_headers = text.count("diff --git")
    if git_headers > file_sections:
        text = _normalize_patch_structure(text)
    elif file_sections and git_headers < file_sections:
        text = _ensure_git_headers(text)
    return text


def sanitize_model_patch(raw: str) -> str:
    """
    Strip markdown fences and common wrappers models add around diffs.

    Real models often wrap patches in ```diff ... ``` — git apply rejects that.

    Critically, this must be **byte-faithful to the diff body**: a unified diff's
    blank *context* line is a single space (" "), and a hunk that ends on one is
    valid git output. A naive ``raw.strip()`` deletes that trailing space line,
    truncating the final hunk and producing "corrupt patch at line N" — the
    `014222Z` re-grade attrition. So we only trim lines that are genuinely *not*
    diff body: fully-empty ("") lines and markdown fence ("```") lines at the
    edges. Any line beginning with a space (a context line, including the bare " "
    blank-context line) is preserved.
    """
    if not raw:
        return raw

    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    fence = re.match(r"^\s*```(?:\w+)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    lines = text.split("\n")

    def _is_edge_junk(ln: str) -> bool:
        # Empty separator lines and markdown fences are not diff body; a bare
        # " " (blank context line) or any "<space>..." context line is.
        return ln == "" or ln.strip() == "```"

    while lines and _is_edge_junk(lines[0]):
        lines.pop(0)
    while lines and _is_edge_junk(lines[-1]):
        lines.pop()

    if not lines:
        return ""
    return "\n".join(lines) + "\n"

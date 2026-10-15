"""Turn page text into a stable form and describe what changed between two versions."""

import difflib

# A map value holds at most 1 MiB, and a snapshot must fit in one.
MAX_TEXT_CHARS = 100_000
# Enough context for the judge without paying for a whole page of tokens.
MAX_DIFF_CHARS = 12_000


def normalize_text(raw: str) -> str:
    """Collapse whitespace and drop blank lines, so layout shifts do not count as changes."""
    lines = (" ".join(line.split()) for line in raw.splitlines())
    return "\n".join(line for line in lines if line)[:MAX_TEXT_CHARS]


def text_diff(old: str, new: str) -> str:
    """Removed lines start with -, added lines with +, with one line of context."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1)
    diff = "\n".join(list(lines)[2:])
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    return diff[:MAX_DIFF_CHARS] + "\n[diff truncated]"

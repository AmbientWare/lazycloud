from __future__ import annotations


def shell_quote(value: str) -> str:
    if value == "":
        return "''"
    return "'" + value.replace("'", "'\\''") + "'"

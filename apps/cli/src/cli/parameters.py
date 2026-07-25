from __future__ import annotations

from collections.abc import Iterable


def parse_key_values(values: Iterable[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            msg = f"expected KEY=VALUE, got {value!r}"
            raise ValueError(msg)
        key, item = value.split("=", 1)
        parsed[key] = item
    return parsed

from __future__ import annotations


def parse_memory_mib(value: str | int | float | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return int(value)
    normalized = value.strip().lower()
    units: dict[str, float] = {
        "ki": 1 / 1024,
        "k": 1 / 1024,
        "mi": 1,
        "m": 1,
        "mb": 1,
        "gi": 1024,
        "g": 1024,
        "gb": 1024,
        "ti": 1024 * 1024,
        "t": 1024 * 1024,
        "tb": 1024 * 1024,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.endswith(suffix):
            return int(float(normalized[: -len(suffix)]) * multiplier)
    return int(float(normalized))

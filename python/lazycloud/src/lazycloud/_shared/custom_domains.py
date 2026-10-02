from __future__ import annotations

import re

MAX_HOSTNAME_LENGTH = 253
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = rf"{_LABEL}(?:\.{_LABEL})+"
_EXACT_HOSTNAME_PATTERN = re.compile(rf"^{_DOMAIN}$")


def normalize_assignable_hostname(value: str) -> str:
    """Accept a concrete hostname a deployment can serve. Wildcards are not one."""

    hostname = value.strip().rstrip(".").lower()
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise ValueError(f"hostname must be at most {MAX_HOSTNAME_LENGTH} characters")
    if not _EXACT_HOSTNAME_PATTERN.fullmatch(hostname):
        raise ValueError(f"hostname must be a concrete domain name: {value!r}")
    return hostname


__all__ = [
    "MAX_HOSTNAME_LENGTH",
    "normalize_assignable_hostname",
]

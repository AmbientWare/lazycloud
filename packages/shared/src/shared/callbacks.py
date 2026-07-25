from __future__ import annotations

from urllib.parse import urlsplit


def normalize_callback_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"}:
        msg = "callback_url must use http or https"
        raise ValueError(msg)
    if not parsed.hostname:
        msg = "callback_url must include a hostname"
        raise ValueError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = "callback_url must not contain credentials"
        raise ValueError(msg)
    if parsed.fragment:
        msg = "callback_url must not contain a fragment"
        raise ValueError(msg)
    try:
        _ = parsed.port
    except ValueError as exc:
        msg = "callback_url has an invalid port"
        raise ValueError(msg) from exc
    return normalized


__all__ = ["normalize_callback_url"]

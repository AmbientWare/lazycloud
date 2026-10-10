from __future__ import annotations


def normalize_mount_prefix(value: str) -> str:
    normalized = value.strip().lstrip("/")
    if not normalized:
        return ""
    if ".." in normalized.split("/"):
        msg = "mount prefix cannot contain '..' path segments"
        raise ValueError(msg)
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


__all__ = [
    "normalize_mount_prefix",
]

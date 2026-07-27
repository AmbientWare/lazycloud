from __future__ import annotations

import hashlib


def normalize_runtime_path(path: str | None) -> str:
    return path or ""


def runtime_path_digest(path: str | None) -> str:
    normalized = normalize_runtime_path(path)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


__all__ = ["normalize_runtime_path", "runtime_path_digest"]

from __future__ import annotations

import hashlib


def normalize_artifact_path(path: str | None) -> str:
    return path or ""


def artifact_path_digest(path: str | None) -> str:
    normalized = normalize_artifact_path(path)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


__all__ = ["artifact_path_digest", "normalize_artifact_path"]

from __future__ import annotations

from shared.enums import StringEnum


class MountAuthMode(StringEnum):
    Ambient = "ambient"
    SecretReferences = "secret_references"


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


def infer_mount_auth_mode(
    access_key_secret: str | None,
    secret_key_secret: str | None,
) -> MountAuthMode:
    has_access_key = bool(access_key_secret)
    has_secret_key = bool(secret_key_secret)
    if has_access_key != has_secret_key:
        msg = "access_key and secret_key must both be set or both be omitted"
        raise ValueError(msg)
    if has_access_key:
        return MountAuthMode.SecretReferences
    return MountAuthMode.Ambient


__all__ = [
    "MountAuthMode",
    "infer_mount_auth_mode",
    "normalize_mount_prefix",
]

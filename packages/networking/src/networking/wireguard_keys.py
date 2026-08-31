from __future__ import annotations

from collections.abc import Mapping

from networking.wireguard import (
    derive_wireguard_public_key,
    generate_wireguard_private_key,
)

SERVER_PRIVATE_KEY = "LAZYCLOUD_WIREGUARD_SERVER_PRIVATE_KEY"
SERVER_PUBLIC_KEY = "LAZYCLOUD_WIREGUARD_SERVER_PUBLIC_KEY"


def platform_private_key(index: int) -> str:
    return f"LAZYCLOUD_WIREGUARD_PLATFORM_{index}_PRIVATE_KEY"


def platform_public_key(index: int) -> str:
    return f"LAZYCLOUD_WIREGUARD_PLATFORM_{index}_PUBLIC_KEY"


def ensure_wireguard_key_document(
    existing: Mapping[str, object],
    *,
    platform_peers: int,
) -> dict[str, str]:
    if platform_peers < 1 or platform_peers > 32:
        raise ValueError("platform_peers must be between 1 and 32")
    values = {key: str(value) for key, value in existing.items()}
    _ensure_key_pair(values, SERVER_PRIVATE_KEY, SERVER_PUBLIC_KEY)
    for index in range(platform_peers):
        _ensure_key_pair(
            values,
            platform_private_key(index),
            platform_public_key(index),
        )
    return values


def _ensure_key_pair(values: dict[str, str], private_name: str, public_name: str) -> None:
    private_key = values.get(private_name, "").strip()
    public_key = values.get(public_name, "").strip()
    if not private_key:
        private_key = generate_wireguard_private_key()
        values[private_name] = private_key
    derived = derive_wireguard_public_key(private_key)
    if public_key and public_key != derived:
        raise RuntimeError(f"{public_name} does not match its private key")
    values[public_name] = derived

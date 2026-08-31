from __future__ import annotations

import argparse
import os
from pathlib import Path

from networking.wireguard_keys import (
    SERVER_PRIVATE_KEY,
    SERVER_PUBLIC_KEY,
    ensure_wireguard_key_document,
    platform_private_key,
    platform_public_key,
)


def _write_key(path: Path, value: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        current = path.read_text(encoding="utf-8").strip()
        if current != value:
            raise RuntimeError(f"refusing to overwrite existing WireGuard key {path}")
        os.chmod(path, mode)
        return
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap_local(directory: Path, *, platform_peers: int) -> None:
    existing: dict[str, str] = {}
    paths = {
        SERVER_PRIVATE_KEY: directory / "server" / "private-key",
        SERVER_PUBLIC_KEY: directory / "server" / "public-key",
    }
    for index in range(platform_peers):
        paths[platform_private_key(index)] = directory / f"platform-{index}" / "private-key"
        paths[platform_public_key(index)] = directory / f"platform-{index}" / "public-key"
    for name, path in paths.items():
        if path.exists():
            existing[name] = path.read_text(encoding="utf-8").strip()
    values = ensure_wireguard_key_document(existing, platform_peers=platform_peers)
    for name, path in paths.items():
        _write_key(path, values[name], mode=0o600 if name.endswith("PRIVATE_KEY") else 0o644)
    print(f"WireGuard keys ready in {directory}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize local durable WireGuard keys.")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--platform-peers", type=int, default=2)
    args = parser.parse_args()
    if args.platform_peers < 1 or args.platform_peers > 32:
        parser.error("--platform-peers must be between 1 and 32")
    bootstrap_local(args.directory, platform_peers=args.platform_peers)


if __name__ == "__main__":
    main()

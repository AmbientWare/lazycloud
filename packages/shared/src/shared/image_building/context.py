from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from shared.image_building.constants import DEFAULT_CONTEXT_IGNORES


class DigestWriter(Protocol):
    def update(self, data: bytes, /) -> None: ...


def fingerprint_build_context(
    context_dir: str | Path,
    *,
    ignored_names: Iterable[str] = DEFAULT_CONTEXT_IGNORES,
) -> str:
    root = Path(context_dir)
    if not root.exists() or not root.is_dir():
        msg = f"build context directory does not exist: {root}"
        raise FileNotFoundError(msg)

    ignored = set(ignored_names)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        metadata = path.lstat()
        _update_digest(digest, relative.as_posix())
        _update_digest(digest, f"{stat.S_IMODE(metadata.st_mode):04o}")
        if stat.S_ISLNK(metadata.st_mode):
            _update_digest(digest, "symlink")
            _update_digest(digest, os.readlink(path))
        elif stat.S_ISDIR(metadata.st_mode):
            _update_digest(digest, "directory")
        elif stat.S_ISREG(metadata.st_mode):
            _update_digest(digest, "file")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            raise ValueError(f"unsupported build context entry: {relative.as_posix()}")
    return digest.hexdigest()


def _update_digest(digest: DigestWriter, value: str) -> None:
    digest.update(value.encode("utf-8"))
    digest.update(b"\0")

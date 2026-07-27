"""Load a developer `.env` at process start.

Settings classes read the environment, which every deployment sets directly
through Compose, Helm, or the provider bootstrap. `.env` is a convenience for
running a process from a host shell, so it is loaded here, by the entrypoint
that owns the process, rather than by each settings class. A library that read
it would resolve the file against whatever directory it happened to be called
from and pull the developer's endpoints into any process that imported it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENVIRONMENT_FILE = ".env"


def load_environment_file(path: str | Path = DEFAULT_ENVIRONMENT_FILE) -> dict[str, str]:
    """Apply `path` to the environment without overriding what is already set.

    An explicitly exported variable and a deployment's own configuration both
    outrank the file, so running inside a container behaves the same whether or
    not one happens to be present.
    """
    source = Path(path)
    if not source.is_file():
        return {}
    applied: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        name, separator, value = entry.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name or name in os.environ:
            continue
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
            cleaned = cleaned[1:-1]
        os.environ[name] = cleaned
        applied[name] = cleaned
    return applied


__all__ = ["DEFAULT_ENVIRONMENT_FILE", "load_environment_file"]

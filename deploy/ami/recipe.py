"""Identity of the host runtime shared by connected-AWS node images."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from agent.operations import build_agent_install_script

_ROOT = Path(__file__).resolve().parents[2]
_BAKE_PATH = _ROOT / "deploy/ami/bake.py"
_GVISOR_VERSION_PATH = _ROOT / "deploy/ami/gvisor-version"


def gvisor_version() -> str:
    version = _GVISOR_VERSION_PATH.read_text(encoding="utf-8").strip()
    if not version or any(character not in "0123456789." for character in version):
        raise ValueError("deploy/ami/gvisor-version is invalid")
    return version


def host_recipe_document() -> bytes:
    runtime_installer = build_agent_install_script().encode()
    document: dict[str, str | int] = {
        "bake_sha256": sha256(_BAKE_PATH.read_bytes()).hexdigest(),
        "gvisor_version": gvisor_version(),
        "runtime_installer_sha256": sha256(runtime_installer).hexdigest(),
        "schema_version": 1,
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def host_recipe_sha256() -> str:
    return sha256(host_recipe_document()).hexdigest()


__all__ = ["gvisor_version", "host_recipe_document", "host_recipe_sha256"]

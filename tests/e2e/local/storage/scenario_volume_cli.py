"""Exercise create, copy, list, move, download, remove, and delete for one Volume.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. All mutations use the public CLI and the uniquely named Volume is
deleted even on failure.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import TypeAdapter
from shared.http.volumes import VolumeInstance
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

ROOT = Path(__file__).resolve().parents[4]
VOLUME_LIST = TypeAdapter(list[VolumeInstance])


def _cli(workspace: str, *arguments: str) -> str:
    return run_text_process(
        ("uv", "run", "lazycloud", "--json", *arguments, "--workspace", workspace),
        cwd=ROOT,
        environment=os.environ,
        timeout=120,
    ).stdout


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Volume CLI")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    workspace = profile.workspace
    volume = f"volume-cli-{secrets.token_hex(6)}"
    try:
        created = VolumeInstance.model_validate_json(_cli(workspace, "volume", "create", volume))
        if created.name != volume:
            raise RuntimeError("Volume CLI created the wrong resource")
        with tempfile.TemporaryDirectory(prefix="lazycloud-volume-cli-") as directory:
            root = Path(directory)
            source = root / "source.txt"
            downloaded = root / "downloaded.txt"
            marker = f"volume-cli-{secrets.token_hex(8)}"
            source.write_text(marker, encoding="utf-8")
            _cli(workspace, "cp", str(source), f"lazycloud://{volume}/incoming/source.txt")
            listed = _cli(workspace, "ls", f"lazycloud://{volume}/incoming")
            if "source.txt" not in listed:
                raise RuntimeError("Volume CLI listing omitted the uploaded file")
            _cli(
                workspace,
                "mv",
                f"lazycloud://{volume}/incoming",
                f"lazycloud://{volume}/accepted",
            )
            _cli(
                workspace,
                "cp",
                f"lazycloud://{volume}/accepted/source.txt",
                str(downloaded),
            )
            if downloaded.read_text(encoding="utf-8") != marker:
                raise RuntimeError("Volume CLI downloaded the wrong bytes")
            _cli(workspace, "rm", f"lazycloud://{volume}/accepted")
        print(json.dumps({"capability": "storage.volume-cli", "volume": volume}))
    finally:
        existing = {
            item.name for item in VOLUME_LIST.validate_json(_cli(workspace, "volume", "list"))
        }
        if volume in existing:
            _cli(workspace, "volume", "delete", volume, "--yes")
        remaining = {
            item.name for item in VOLUME_LIST.validate_json(_cli(workspace, "volume", "list"))
        }
        if volume in remaining:
            raise RuntimeError("Volume CLI cleanup left its unique resource")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

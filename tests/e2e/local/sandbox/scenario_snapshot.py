"""Snapshot one Sandbox filesystem and relaunch it as a new Sandbox.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. Both runtime containers are terminated through the public SDK.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence

from lazycloud.abstractions.sandbox import SandboxInstance
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import App, Image


def main(argv: Sequence[str] | None = None) -> int:
    try:
        require_live(argv, description=__doc__ or "Sandbox snapshot")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"sandbox-snapshot-{secrets.token_hex(8)}"
    source_app = App(f"sandbox_snapshot_source_{secrets.token_hex(6)}")
    source = source_app.sandbox(
        name="source",
        image=Image(python_version="3.12"),
        keep_warm_seconds=120,
    )
    source_instance: SandboxInstance | None = None
    restored_instance: SandboxInstance | None = None
    try:
        source_instance = source.create(timeout_seconds=120)
        write = source_instance.run(
            ["sh", "-lc", f"printf '%s\\n' {marker} > /workspace/accepted.txt"],
            timeout_seconds=30,
        )
        if write.exit_code != 0:
            raise RuntimeError("snapshot source write failed")
        image_id = source_instance.create_image_from_filesystem()
        if not image_id:
            raise RuntimeError("Sandbox snapshot returned no image ID")
        if not source_instance.terminate():
            raise RuntimeError("snapshot source termination did not report success")
        source_instance = None

        restored_app = App(f"sandbox_snapshot_restored_{secrets.token_hex(6)}")
        restored = restored_app.sandbox(
            name="restored",
            image=Image.from_id(image_id),
            keep_warm_seconds=120,
        )
        restored_instance = restored.create(timeout_seconds=120)
        read = restored_instance.run(["cat", "/workspace/accepted.txt"], timeout_seconds=30)
        if read.exit_code != 0 or marker not in read.stdout:
            raise RuntimeError("restored Sandbox omitted its snapshot marker")
        print(
            json.dumps(
                {
                    "capability": "sandbox.snapshot-relaunch",
                    "container_id": restored_instance.container_id,
                    "image_id": image_id,
                }
            )
        )
    finally:
        if restored_instance is not None and not restored_instance.terminate():
            raise RuntimeError("restored Sandbox termination did not report success")
        if source_instance is not None and not source_instance.terminate():
            raise RuntimeError("source Sandbox termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

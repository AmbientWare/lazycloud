"""Create, execute in, and terminate one Sandbox through the public SDK.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The only created runtime resource is uniquely named and is terminated in
cleanup.
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
        require_live(argv, description=__doc__ or "Sandbox lifecycle")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"sandbox-lifecycle-{secrets.token_hex(8)}"
    app = App(f"sandbox_lifecycle_{secrets.token_hex(6)}")
    sandbox = app.sandbox(
        name="lifecycle",
        image=Image(python_version="3.12"),
        keep_warm_seconds=120,
        memory="256Mi",
    )
    instance: SandboxInstance | None = None
    try:
        instance = sandbox.create(timeout_seconds=120)
        result = instance.run(["sh", "-lc", f"printf '%s\\n' {marker}"], timeout_seconds=30)
        if result.exit_code != 0 or marker not in result.stdout:
            raise RuntimeError("Sandbox command did not return its marker")
        print(
            json.dumps(
                {
                    "capability": "sandbox.lifecycle",
                    "container_id": instance.container_id,
                }
            )
        )
    finally:
        if instance is not None and not instance.terminate():
            raise RuntimeError("Sandbox termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

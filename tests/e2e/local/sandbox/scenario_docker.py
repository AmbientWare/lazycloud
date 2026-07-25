"""Run Docker inside one Docker-enabled Sandbox.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The uniquely named Sandbox is terminated in cleanup.
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
        require_live(argv, description=__doc__ or "Sandbox Docker")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"sandbox-docker-{secrets.token_hex(8)}"
    app = App(f"sandbox_docker_{secrets.token_hex(6)}")
    sandbox = app.sandbox(
        name="docker",
        image=Image().with_docker(),
        docker_enabled=True,
        keep_warm_seconds=120,
        memory="2Gi",
    )
    instance: SandboxInstance | None = None
    try:
        instance = sandbox.create(timeout_seconds=180)
        info = instance.run(["docker", "info"], timeout_seconds=30)
        if info.exit_code != 0:
            raise RuntimeError("docker info failed inside the Sandbox")
        result = instance.docker.run(
            "alpine:3.20",
            ["sh", "-lc", f"printf '%s\\n' {marker}"],
            remove=True,
        )
        if not result.success or marker not in result.stdout:
            raise RuntimeError("nested docker run did not return its marker")
        print(
            json.dumps(
                {
                    "capability": "sandbox.docker",
                    "container_id": instance.container_id,
                }
            )
        )
    finally:
        if instance is not None and not instance.terminate():
            raise RuntimeError("Docker Sandbox termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run one uploaded Compose service inside a Docker-enabled Sandbox.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The Compose project is brought down and its uniquely named Sandbox is
terminated.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from collections.abc import Sequence
from pathlib import Path

from lazycloud.abstractions.sandbox import DockerComposeStack, SandboxInstance
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import App, Image


def main(argv: Sequence[str] | None = None) -> int:
    try:
        require_live(argv, description=__doc__ or "Sandbox Compose")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"sandbox-compose-{secrets.token_hex(8)}"
    app = App(f"sandbox_compose_{secrets.token_hex(6)}")
    sandbox = app.sandbox(
        name="compose",
        image=Image().with_docker(),
        docker_enabled=True,
        keep_warm_seconds=120,
        memory="2Gi",
    )
    instance: SandboxInstance | None = None
    stack: DockerComposeStack | None = None
    try:
        instance = sandbox.create(timeout_seconds=180)
        with tempfile.TemporaryDirectory(prefix="lazycloud-sandbox-compose-") as directory:
            compose_file = Path(directory) / "compose.yaml"
            compose_file.write_text(
                "services:\n"
                "  probe:\n"
                "    image: alpine:3.20\n"
                f'    command: ["sh", "-lc", "echo {marker}; sleep 120"]\n',
                encoding="utf-8",
            )
            instance.fs.upload_file(compose_file, "/workspace/compose.yaml")
        stack = instance.docker.compose_up(file="/workspace/compose.yaml")
        if not stack.result.success:
            raise RuntimeError("Sandbox Compose up failed")
        if "probe" not in stack.ps() or marker not in stack.logs("probe", tail=20):
            raise RuntimeError("Sandbox Compose service omitted its public evidence")
        print(
            json.dumps(
                {
                    "capability": "sandbox.compose",
                    "container_id": instance.container_id,
                }
            )
        )
    finally:
        if instance is not None and stack is not None:
            result = instance.docker.compose_down(
                file="/workspace/compose.yaml",
                volumes=True,
                remove_orphans=True,
            )
            if not result.success:
                raise RuntimeError("Sandbox Compose cleanup failed")
        if instance is not None and not instance.terminate():
            raise RuntimeError("Compose Sandbox termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

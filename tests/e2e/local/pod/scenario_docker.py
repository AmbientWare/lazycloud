"""Run a Docker-enabled Pod command and retain its output and nonzero exit status.

Requires an authenticated public profile targeting the healthy local stack.
The uniquely named Pod is stopped through the public SDK in cleanup.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from urllib.parse import urlparse

from lazycloud.abstractions.pod import Container, PodInstance
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

from lazycloud import App, Image


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Pod Docker")
        hostname = urlparse(profile.resolved_endpoint()).hostname or ""
        if hostname not in {"localhost", "127.0.0.1"} and not hostname.endswith(".localhost"):
            raise LivePrerequisiteError("Pod Docker acceptance requires the local stack")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"pod-docker-{secrets.token_hex(8)}"
    app = App(f"pod_docker_{secrets.token_hex(6)}")
    pod = app.pod(
        name="docker",
        image=Image().with_docker(),
        docker_enabled=True,
        memory="2Gi",
        keep_warm=0,
    )
    instance: PodInstance | None = None
    try:
        instance = pod.run(
            "sh",
            "-c",
            f"docker info >/dev/null && docker run --rm alpine:3.20 echo {marker} || exit 1; "
            "exit 23",
            timeout_seconds=180,
        )
        print(json.dumps({"container_id": instance.container_id}), flush=True)
        result = Container(container_id=instance.container_id).attach()
        if not result.done or result.exit_code != 23 or marker not in result.output:
            raise RuntimeError("Docker Pod did not preserve nested Docker output and command exit")
        print(json.dumps({"capability": "pod.docker", "exit_code": result.exit_code}))
    finally:
        if instance is not None and not instance.terminate():
            raise RuntimeError("Docker Pod termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

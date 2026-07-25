"""Keep one Sandbox exposed-port route valid across a control-plane restart.

Requires an authenticated public lazycloud profile targeting a healthy root
Compose stack. The uniquely named Sandbox is terminated in cleanup.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
from lazycloud.abstractions.sandbox import SandboxInstance, SandboxProcess
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

from lazycloud import App, Image

ROOT = Path(__file__).resolve().parents[4]
PORT = 18080


def _await_marker(url: str, marker: str, token: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        except httpx.HTTPError:
            time.sleep(0.25)
            continue
        if response.status_code == 200 and response.text == marker:
            return
        time.sleep(0.25)
    raise RuntimeError("Sandbox exposed port did not return its marker")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Sandbox exposed-port restart")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"sandbox-port-{secrets.token_hex(8)}"
    app = App(f"sandbox_port_{secrets.token_hex(6)}")
    sandbox = app.sandbox(
        name="exposed-port",
        image=Image(python_version="3.12"),
        authorized=True,
        keep_warm_seconds=180,
        memory="256Mi",
    )
    instance: SandboxInstance | None = None
    server: SandboxProcess | None = None
    try:
        instance = sandbox.create(timeout_seconds=120)
        setup = instance.run(
            [
                "sh",
                "-lc",
                f"mkdir -p /workspace/public && printf '%s' {marker} "
                "> /workspace/public/index.html",
            ],
            timeout_seconds=30,
        )
        if setup.exit_code != 0:
            raise RuntimeError("Sandbox exposed-port marker setup failed")
        server = instance.process.exec(
            "python3",
            "-m",
            "http.server",
            str(PORT),
            "--bind",
            "0.0.0.0",
            "--directory",
            "/workspace/public",
        )
        url = instance.expose_port(PORT)
        _await_marker(url, marker, profile.token)
        run_text_process(
            ("docker", "compose", "restart", "control-plane"),
            cwd=ROOT,
            environment=os.environ,
            timeout=120,
        )
        if instance.list_urls() != {PORT: url}:
            raise RuntimeError("Sandbox exposed-port route changed after restart")
        _await_marker(url, marker, profile.token)
        print(
            json.dumps(
                {
                    "capability": "sandbox.exposed-port-restart",
                    "container_id": instance.container_id,
                    "url": url,
                }
            )
        )
    finally:
        if server is not None:
            try:
                server.kill()
                server.wait(timeout=5)
            except Exception:
                if instance is None:
                    raise
        if instance is not None and not instance.terminate():
            raise RuntimeError("exposed-port Sandbox termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

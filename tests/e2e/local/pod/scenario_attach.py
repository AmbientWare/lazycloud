"""Attach the public CLI to one running Pod container.

Requires an authenticated public lazycloud profile targeting the healthy local
stack. The uniquely named Pod is terminated through the public SDK.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from lazycloud.abstractions.pod import PodInstance
from pydantic import TypeAdapter
from tests.e2e._support.process import (
    LivePrerequisiteError,
    blocked,
    require_live,
    run_text_process,
)

from lazycloud import App, Image

ROOT = Path(__file__).resolve().parents[4]
JSON_OBJECT = TypeAdapter(dict[str, object])


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Pod attach")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    marker = f"pod-attach-{secrets.token_hex(8)}"
    app = App(f"pod_attach_{secrets.token_hex(6)}")
    pod = app.pod(
        name="attach",
        image=Image(python_version="3.12"),
        keep_warm=120,
    )
    instance: PodInstance | None = None
    try:
        instance = pod.run(
            "sh",
            "-lc",
            f"printf '%s\\n' {marker}; sleep 15",
            timeout_seconds=120,
        )
        result = run_text_process(
            (
                "uv",
                "run",
                "lazycloud",
                "--json",
                "container",
                "attach",
                instance.container_id,
                "--workspace",
                profile.workspace,
            ),
            cwd=ROOT,
            environment=os.environ,
            timeout=120,
            secrets=(profile.token,),
        )
        payload = JSON_OBJECT.validate_json(result.stdout)
        output = payload.get("output")
        if payload.get("done") is not True or payload.get("exit_code") != 0:
            raise RuntimeError("Pod attach did not reach a successful terminal state")
        if not isinstance(output, str) or marker not in output:
            raise RuntimeError("Pod attach output omitted its marker")
        print(
            json.dumps(
                {
                    "capability": "pod.attach",
                    "container_id": instance.container_id,
                }
            )
        )
    finally:
        if instance is not None and not instance.terminate():
            raise RuntimeError("Pod termination did not report success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

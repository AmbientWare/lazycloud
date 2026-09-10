"""Check concurrent function logs and final partial output through the local SDK.

Requires an authenticated profile and a healthy local stack built from current source.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from lazycloud.cli.control import resource_client
from shared.tasks import is_terminal_task_status
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "Function logs")
        hostname = urlparse(profile.resolved_endpoint()).hostname or ""
        if hostname not in {"localhost", "127.0.0.1"} and not hostname.endswith(".localhost"):
            raise LivePrerequisiteError("Function log acceptance requires the local stack")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_logs import APP_NAME, app, emit

    client = resource_client(workspace=profile.workspace)
    try:
        app.deploy(workspace=profile.workspace, source_root=Path(__file__).resolve().parent)
        for values in ([0], [1, 2]):
            calls = [emit.spawn(value) for value in values]
            for cycle in range(120):
                states = [call.task.get() for call in calls]
                print(
                    json.dumps(
                        {
                            "cycle": cycle,
                            "tasks": [
                                {
                                    "id": state.id,
                                    "status": state.status.value,
                                    "container": state.container_id,
                                    "logs": len(call.logs(limit=1000)),
                                }
                                for call, state in zip(calls, states, strict=True)
                            ],
                        }
                    ),
                    flush=True,
                )
                if all(is_terminal_task_status(state.status) for state in states):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Function log acceptance exceeded 120 diagnostic cycles")
            answers = [call.get(timeout_seconds=1) for call in calls]
            for value, call, answer in zip(values, calls, answers, strict=True):
                if answer["task_id"] != call.task_id:
                    raise RuntimeError("Concurrent invocation received another task's identity")
                logs = call.logs(limit=1000)
                stdout = [entry.message for entry in logs if entry.stream == "stdout"]
                stderr = [entry.message for entry in logs if entry.stream == "stderr"]
                if stdout != [f"{value}:stdout:{i}" for i in range(64)] + [f"{value}:partial"]:
                    raise RuntimeError("Completed function lost or crossed stdout records")
                if stderr != [f"{value}:stderr"]:
                    raise RuntimeError("Completed function lost or crossed stderr records")
                expected = [("stdout", f"{value}:stdout:{i}") for i in range(64)] + [
                    ("stderr", f"{value}:stderr"),
                    ("stdout", f"{value}:partial"),
                ]
                if [(entry.stream, entry.message) for entry in logs] != expected:
                    raise RuntimeError("Function stdout and stderr records changed order")
            if len(answers) > 1:
                if len({answer["pid"] for answer in answers}) != 1:
                    raise RuntimeError("Function calls did not share an interpreter")
                if max(float(answer["started"]) for answer in answers) >= min(
                    float(answer["finished"]) for answer in answers
                ):
                    raise RuntimeError("Function calls did not overlap")
            print(json.dumps({"capability": "function.logs", "answers": answers}), flush=True)
    finally:
        matches = [item for item in client.list_apps(active=True).data if item.name == APP_NAME]
        if len(matches) > 1:
            raise RuntimeError("Function acceptance app name resolved more than once")
        for item in matches:
            client.delete_app(item.id)
        if any(item.name == APP_NAME for item in client.list_apps(active=True).data):
            raise RuntimeError("Function acceptance app remained active after cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

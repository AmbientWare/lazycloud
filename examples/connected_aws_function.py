from __future__ import annotations

import json
from typing import NamedTuple, Protocol, TypedDict

from lazycloud import App, FunctionCall, Image


class ProbeResult(TypedDict):
    marker: str
    doubled: int
    status: str


class ConnectedAwsProbe(Protocol):
    def spawn(self, marker: str, value: int = 21) -> FunctionCall[ProbeResult]: ...


class ConnectedAwsProbeApp(NamedTuple):
    app: App
    probe: ConnectedAwsProbe


def _deterministic_probe(marker: str, value: int = 21) -> ProbeResult:
    result: ProbeResult = {
        "marker": marker,
        "doubled": value * 2,
        "status": "complete",
    }
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def connected_aws_probe(app_slug: str) -> ConnectedAwsProbeApp:
    app = App(app_slug)
    probe = app.function(
        _deterministic_probe,
        name="deterministic-probe",
        image=Image(python_version="3.12"),
        cpu=0.25,
        memory="128Mi",
        timeout_seconds=600,
    )
    return ConnectedAwsProbeApp(app=app, probe=probe)


app = App("connected_aws_smoke")
deterministic_probe = app.function(
    _deterministic_probe,
    name="deterministic-probe",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    timeout_seconds=600,
)


__all__ = ["ConnectedAwsProbeApp", "connected_aws_probe", "deterministic_probe"]

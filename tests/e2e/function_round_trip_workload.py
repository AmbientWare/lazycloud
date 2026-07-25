from __future__ import annotations

import json
from typing import Protocol, TypedDict

from lazycloud import App, FunctionCall, Image


class RoundTripResult(TypedDict):
    marker: str
    doubled: int


class RoundTripProbe(Protocol):
    def spawn(self, marker: str, value: int) -> FunctionCall[RoundTripResult]: ...


def _round_trip(marker: str, value: int) -> RoundTripResult:
    result: RoundTripResult = {"marker": marker, "doubled": value * 2}
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def round_trip_app(name: str) -> tuple[App, RoundTripProbe]:
    app = App(name)
    function = app.function(
        _round_trip,
        name="round-trip",
        image=Image(python_version="3.12"),
        cpu=0.25,
        memory="128Mi",
        timeout_seconds=600,
    )
    return app, function


__all__ = ["RoundTripResult", "round_trip_app"]

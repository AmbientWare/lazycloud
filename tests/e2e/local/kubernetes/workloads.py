"""Workloads used only by the prepared-cluster capacity scenarios."""

from __future__ import annotations

import os
import time

from lazycloud import App

app = App(os.environ["LAZYCLOUD_E2E_KUBERNETES_APP"])


@app.function(
    name="scale-probe",
    cpu=3.5,
    memory="6Gi",
    preemptible=True,
)
def scale_probe(delay_seconds: int) -> dict[str, int]:
    time.sleep(delay_seconds)
    return {"delay_seconds": delay_seconds}


@app.function(
    name="maximum-probe",
    cpu=3.5,
    memory="6Gi",
    preemptible=True,
)
def maximum_probe() -> str:
    return "unexpected-capacity"


__all__ = ["maximum_probe", "scale_probe"]

from __future__ import annotations

import os
import time

from lazycloud import App, Autoscaler, Image

APP_NAME = os.environ.get("LAZYCLOUD_SCALING_APP", "scaling_acceptance")
app = App(APP_NAME)
image = Image(python_version="3.12")
MACHINE = "compose-agent"


@app.function(
    name="interactive",
    machine=MACHINE,
    image=image,
    cpu=0.5,
    memory="512Mi",
    concurrency=4,
    keep_warm=180,
    autoscaler=Autoscaler(max_containers=2),
    retries=0,
)
def interactive(sequence: int, delay: float = 0.02) -> int:
    time.sleep(delay)
    return sequence


@app.function(
    name="nested",
    machine=MACHINE,
    image=image,
    cpu=0.25,
    memory="256Mi",
    keep_warm=180,
    autoscaler=Autoscaler(max_containers=1),
    env={"LAZYCLOUD_SCALING_APP": APP_NAME},
    retries=0,
)
def nested(sequence: int) -> int:
    return interactive.remote(sequence)


@app.endpoint(
    name="http",
    machine=MACHINE,
    route="/echo",
    methods=["POST"],
    image=image,
    cpu=0.25,
    memory="256Mi",
    concurrency=16,
    keep_warm=180,
    autoscaler=Autoscaler(max_containers=1),
)
def echo(sequence: int) -> dict[str, int]:
    return {"sequence": sequence}


sandbox = app.sandbox(name="commands", image=image, cpu=0.5, memory="256Mi", machine=MACHINE)

"""Replay a recorded mixed load against a live LazyCloud stack.

The functions carry the resources the recording shows for each name; each task
sleeps its recorded runtime. Arrivals are submitted at their recorded offsets,
shifted so the first is at zero. How the fleet served them is read from the
stack afterwards with report.sql, not from this client. Run it from this
directory: the app uploads the directory it runs from.

    LAZYCLOUD_ENDPOINT=https://api.lazycloud.dev LAZYCLOUD_TOKEN=... LAZYCLOUD_WORKSPACE=<ws> \\
    cd acceptance/mixedload && uv run python mixedload.py [recording.json] [speed]
    cd acceptance/mixedload && uv run python mixedload.py bursts

speed scales the offsets (2 runs the schedule twice as fast); 1 by default.
bursts sends two bursts of 30 one-CPU tasks 90 seconds apart instead.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from lazycloud import App

RUN = time.strftime("%m%d%H%M")
app = App(f"mixedload_{RUN}")
MINUTES = 3600
# One container per task, gone when the task ends, as the recorded run had.
SCALE = {"min_containers": 0, "max_containers": 200, "tasks_per_container": 1}


def hold(seconds: float) -> dict:
    time.sleep(seconds)
    return {"slept": seconds}


def fleet(cpu: float, memory: str, *, gpu: str | None = None, preemptible: bool = False):
    """A function of the recording's shape: one container per task, no retries."""
    return app.function(
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        preemptible=preemptible,
        timeout_seconds=MINUTES,
        retries=0,
        keep_warm=0,
        autoscaler=SCALE,
    )


@fleet(0.25, "512Mi", preemptible=True)
def quick(seconds: float) -> dict:
    return hold(seconds)


@fleet(1, "2048Mi", preemptible=True)
def medium(seconds: float) -> dict:
    return hold(seconds)


@fleet(1, "2048Mi", preemptible=True)
def long(seconds: float) -> dict:
    return hold(seconds)


@fleet(1, "20480Mi", gpu="A10G")
def gpusmall(seconds: float) -> dict:
    return hold(seconds)


@fleet(3, "20480Mi", gpu="A10G")
def gpubig(seconds: float) -> dict:
    return hold(seconds)


@fleet(2, "12288Mi", gpu="A10G")
def gpuload_small(seconds: float) -> dict:
    return hold(seconds)


@fleet(10, "41728Mi", gpu="A10G")
def gpuload_big(seconds: float) -> dict:
    return hold(seconds)


@fleet(1, "2048Mi")
def smallfiles(seconds: float) -> dict:
    return hold(seconds)


@fleet(1, "4096Mi", gpu="A10G")
def smallfiles_gpu(seconds: float) -> dict:
    return hold(seconds)


FUNCTIONS = {
    f.name or f.func.__name__: f
    for f in (
        quick,
        medium,
        long,
        gpusmall,
        gpubig,
        gpuload_small,
        gpuload_big,
        smallfiles,
        smallfiles_gpu,
    )
}


def arrivals(path: str) -> list[dict]:
    raw = json.loads(Path(path).read_text())
    out = [{"at": a[0] / 1000, "fn": a[1], "runs": a[5] / 1000} for a in raw["arrivals"]]
    first = min(x["at"] for x in out)
    for x in out:
        x["at"] -= first
    return sorted(out, key=lambda x: x["at"])


def bursts(size: int = 30, gap: float = 90, runs: float = 30) -> None:
    """Two bursts of size one-CPU tasks gap seconds apart: the second should land on
    the room the first left."""
    app.deploy()
    for n in range(2):
        if n:
            time.sleep(gap)
        print(f"burst {n + 1} at {time.strftime('%H:%M:%S')}", flush=True)
        calls = [medium.spawn(runs) for _ in range(size)]
        for call in calls:
            call.result()
    print(f"done at {time.strftime('%H:%M:%S')}", flush=True)


def main() -> None:
    if sys.argv[1:2] == ["bursts"]:
        bursts()
        return
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).with_name("recording.json"))
    speed = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    schedule = arrivals(path)
    missing = {a["fn"] for a in schedule} - set(FUNCTIONS)
    if missing:
        sys.exit(f"recording names functions this driver lacks: {sorted(missing)}")
    print(f"deploying mixedload_{RUN}", flush=True)
    app.deploy()
    counts: dict[str, int] = defaultdict(int)
    calls = []
    lock = threading.Lock()
    threads = []
    start = time.monotonic()
    print(f"submitting {len(schedule)} tasks over {schedule[-1]['at'] / speed:.0f}s", flush=True)

    def submit(a: dict) -> None:
        call = FUNCTIONS[a["fn"]].spawn(a["runs"])
        with lock:
            calls.append((a["fn"], call))
            counts[a["fn"]] += 1

    for n, a in enumerate(schedule, 1):
        delay = a["at"] / speed - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)
        t = threading.Thread(target=submit, args=(a,), daemon=True)
        t.start()
        threads.append(t)
        if n % 25 == 0:
            print(f"{time.monotonic() - start:6.0f}s submitted {n}", flush=True)
    for t in threads:
        t.join()
    print(f"submitted {len(calls)}: {dict(counts)}", flush=True)
    print("waiting for the tasks to finish", flush=True)
    failed = 0
    for fn, call in calls:
        try:
            call.result()
        except Exception as e:
            failed += 1
            print(f"{fn}: {e}", flush=True)
    print(f"done: {len(calls) - failed} succeeded, {failed} failed", flush=True)


if __name__ == "__main__":
    main()

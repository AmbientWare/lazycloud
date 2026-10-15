"""Post-Ship smoke check for volumes and cloud buckets on a live stack.

A file written to a volume on an on-demand host is read on a Spot host, and
read again once every mount stopped and the volume mounted afresh. A file of
gigabytes renamed just before its container stops is there under its new
name, and reads in full through a memory map, as model loaders read weights.
A read-only cloud bucket reads a known object and refuses a write. The check
deletes its app and volume at the end, and prints its timings.

Run it from the repository root against the stack Ship deployed, in a
workspace kept for the check:

    LAZYCLOUD_ENDPOINT=https://api.example LAZYCLOUD_TOKEN=... LAZYCLOUD_WORKSPACE=smoke \\
    LAZYCLOUD_SMOKE_BUCKET=<bucket> LAZYCLOUD_SMOKE_BUCKET_REGION=us-east-2 \\
    LAZYCLOUD_SMOKE_BUCKET_KEY=<key> LAZYCLOUD_SMOKE_BUCKET_SHA256=<hex> \\
    uv run python acceptance/smoke/volumes.py

The workspace holds read-only keys for the bucket as the secrets
SMOKE_BUCKET_ACCESS_KEY and SMOKE_BUCKET_SECRET_KEY. The bucket part is
skipped without LAZYCLOUD_SMOKE_BUCKET.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lazycloud import App, CloudBucket, CloudBucketConfig, Volume

# Containers import this module too; they get the run's settings from the
# workloads' environment, so both sides declare the same names.
SMOKE_ENV = {
    name: value for name, value in os.environ.items() if name.startswith("LAZYCLOUD_SMOKE_")
}
RUN = SMOKE_ENV.setdefault("LAZYCLOUD_SMOKE_RUN", uuid.uuid4().hex[:8])
APP_NAME = f"smoke_volumes_{RUN}"
BUCKET = SMOKE_ENV.get("LAZYCLOUD_SMOKE_BUCKET", "")
MOUNT_WAIT_SECONDS = 180
# Renaming a file this large is a server-side copy that outlasts the
# container; its mount stores it within the agent's flush window.
LARGE_FILE_GIB = 6
FLUSH_WAIT_SECONDS = 600

app = App(APP_NAME)
volume = Volume(f"smoke-{RUN}", "/smoke")
bucket = CloudBucket(
    "smoke-bucket",
    "/bucket",
    CloudBucketConfig(
        bucket=BUCKET or None,
        region=SMOKE_ENV.get("LAZYCLOUD_SMOKE_BUCKET_REGION") or None,
        read_only=True,
        access_key="SMOKE_BUCKET_ACCESS_KEY",
        secret_key="SMOKE_BUCKET_SECRET_KEY",
    ),
)


@app.function(volumes=[volume], preemptible=False, env=SMOKE_ENV)
def write(name: str, text: str) -> str:
    Path("/smoke", name).write_text(text)
    return text


@app.function(volumes=[volume], preemptible=True, env=SMOKE_ENV)
def read(name: str) -> str:
    return Path("/smoke", name).read_text()


@app.function(volumes=[volume], preemptible=False, env=SMOKE_ENV)
def write_large(name: str) -> str:
    """Writes LARGE_FILE_GIB under a temporary name and renames it, as downloaders do."""
    chunk = os.urandom(1 << 20)
    partial = Path("/smoke", name + ".incomplete")
    with partial.open("wb") as file:
        for _ in range(LARGE_FILE_GIB << 10):
            file.write(chunk)
    partial.rename(Path("/smoke", name))
    return name


@app.function(volumes=[volume], env=SMOKE_ENV)
def size(name: str) -> int:
    path = Path("/smoke", name)
    return path.stat().st_size if path.exists() else -1


@app.function(volumes=[volume], env=SMOKE_ENV)
def read_mapped(name: str) -> int:
    """Touches every page of name through a memory map and returns its size."""
    with (
        Path("/smoke", name).open("rb") as file,
        mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        for offset in range(0, len(data), mmap.PAGESIZE):
            data[offset]
        return len(data)


@app.function(volumes=[bucket], env=SMOKE_ENV)
def read_bucket(key: str) -> str:
    digest = hashlib.sha256(Path("/bucket", key).read_bytes()).hexdigest()
    try:
        Path("/bucket", f"smoke-{RUN}-refused").write_text("x")
    except OSError:
        return digest
    return "written"


def api(method: str, path: str) -> Any:
    endpoint = os.environ["LAZYCLOUD_ENDPOINT"].rstrip("/")
    workspace = urllib.parse.quote(os.environ["LAZYCLOUD_WORKSPACE"])
    request = urllib.request.Request(
        f"{endpoint}/v1/workspaces/{workspace}{path}",
        method=method,
        # The edge refuses urllib's default user agent.
        headers={
            "Authorization": "Bearer " + os.environ["LAZYCLOUD_TOKEN"],
            "User-Agent": "lazycloud-smoke",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


def containers(*, live: bool) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"app": APP_NAME, "live": str(live).lower(), "limit": 100})
    return api("GET", f"/containers?{query}")["containers"]


def stop_all(deadline_seconds: float) -> None:
    """Stops the app's containers and waits until none is live."""
    deadline = time.monotonic() + deadline_seconds
    while live := containers(live=True):
        for c in live:
            if c["state"] != "draining":
                api("POST", f"/containers/{c['id']}/stop")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{len(live)} containers still live after {deadline_seconds}s")
        time.sleep(2)


def wait_for_size(name: str, want: int) -> int:
    """Returns name's size once it is want, or its last size after FLUSH_WAIT_SECONDS."""
    deadline = time.monotonic() + FLUSH_WAIT_SECONDS
    while (got := size.remote(name)) != want and time.monotonic() < deadline:
        time.sleep(10)
    return got


def timed(timings: dict[str, float], name: str, call: Callable[[], str]) -> str:
    started = time.monotonic()
    try:
        return call()
    finally:
        timings[name] = round(time.monotonic() - started, 2)


def check(timings: dict[str, float]) -> list[str]:
    failures: list[str] = []
    stamp = uuid.uuid4().hex
    timed(timings, "write_on_demand", lambda: write.remote("stamp.txt", stamp))
    if (got := timed(timings, "read_spot", lambda: read.remote("stamp.txt"))) != stamp:
        failures.append(f"the Spot host read {got!r}, want {stamp!r}")

    timed(timings, "stop_mounts", lambda: stop_all(MOUNT_WAIT_SECONDS) or "")
    if (got := timed(timings, "read_remounted", lambda: read.remote("stamp.txt"))) != stamp:
        failures.append(f"after a remount read {got!r}, want {stamp!r}")

    timed(timings, "write_large", lambda: write_large.remote("large.bin"))
    timed(timings, "stop_large", lambda: stop_all(MOUNT_WAIT_SECONDS) or "")
    want_size = LARGE_FILE_GIB << 30
    got_size = int(
        timed(timings, "renamed_large", lambda: str(wait_for_size("large.bin", want_size)))
    )
    if got_size != want_size:
        failures.append(
            f"large.bin has {got_size} bytes after its container stopped, want {want_size}"
        )
    else:
        mapped = int(
            timed(timings, "read_mapped_large", lambda: str(read_mapped.remote("large.bin")))
        )
        if mapped != want_size:
            failures.append(f"a mapped read of large.bin saw {mapped} bytes, want {want_size}")

    if BUCKET:
        key, want = (
            SMOKE_ENV["LAZYCLOUD_SMOKE_BUCKET_KEY"],
            SMOKE_ENV["LAZYCLOUD_SMOKE_BUCKET_SHA256"],
        )
        got = timed(timings, "read_bucket", lambda: read_bucket.remote(key))
        if got == "written":
            failures.append("the read-only cloud bucket took a write")
        elif got != want:
            failures.append(f"the cloud bucket's {key} has digest {got}, want {want}")
    return failures


def cleanup() -> list[str]:
    """Deletes the app, then the volume once no container mounts it."""
    problems: list[str] = []
    try:
        stop_all(MOUNT_WAIT_SECONDS)
        api("DELETE", f"/apps/{urllib.parse.quote(APP_NAME)}")
    except (urllib.error.URLError, RuntimeError) as error:
        problems.append(f"delete app {APP_NAME}: {error}")
    deadline = time.monotonic() + MOUNT_WAIT_SECONDS
    while True:
        try:
            volume.delete()
            return problems
        except Exception as error:
            if time.monotonic() > deadline:
                return [*problems, f"delete volume {volume.name}: {error}"]
            time.sleep(5)


def main() -> int:
    timings: dict[str, float] = {}
    try:
        failures = check(timings)
    except Exception as error:
        failures = [f"check stopped: {error!r}"]
    failures += cleanup()
    print(json.dumps({"run": RUN, "timings_seconds": timings, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

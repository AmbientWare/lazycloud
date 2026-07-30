"""Prove a container's cpu and memory requests are floors inside an enforced disk ceiling.

Prerequisite: an authenticated public lazycloud profile targeting a healthy
root Compose stack. Creates one uniquely named app, drives a single container
past every request it made, and deletes that app through the public resource
API.
"""

from __future__ import annotations

import errno
import json
from collections.abc import Sequence
from pathlib import Path

from lazycloud.cli.control import resource_client
from tests.e2e._support.process import LivePrerequisiteError, blocked, require_live

SOURCE_ROOT = Path(__file__).resolve().parent

# Eight busy processes against an eighth of a core. One whole core is already
# eight times the request while staying far below both the burst ceiling and
# what any host running this stack can supply, so the threshold does not depend
# on the host's core count.
MINIMUM_BURST_CORES = 1.0
# The ceiling a container gets when nothing was requested. Seeing it means the
# request never arrived.
PLATFORM_DEFAULT_DISK_BYTES = 100 * 1024**3


def main(argv: Sequence[str] | None = None) -> int:
    try:
        profile = require_live(argv, description=__doc__ or "container resource bounding")
    except LivePrerequisiteError as exc:
        return blocked(exc)
    from .workload_resource_bounding import (
        ALLOCATE_MIB,
        APP_NAME,
        BURST_PROCESSES,
        BURST_SECONDS,
        FILL_CHUNK_MIB,
        REQUESTED_CORES,
        REQUESTED_DISK,
        REQUESTED_DISK_BYTES,
        REQUESTED_MEMORY,
        app,
        bounded_workload,
    )

    workspace = profile.workspace
    try:
        app.deploy(workspace=workspace, source_root=SOURCE_ROOT)
        observed = bounded_workload.remote(
            processes=BURST_PROCESSES,
            burst_seconds=BURST_SECONDS,
            allocate_mib=ALLOCATE_MIB,
            chunk_mib=FILL_CHUNK_MIB,
        )
        _assert_cpu_request_is_a_floor(observed, requested_cores=REQUESTED_CORES)
        _assert_memory_request_is_a_floor(
            observed, requested=REQUESTED_MEMORY, allocated_mib=ALLOCATE_MIB
        )
        _assert_requested_disk_reached_the_container(
            observed, requested=REQUESTED_DISK, requested_bytes=REQUESTED_DISK_BYTES
        )
        _assert_disk_ceiling_stopped_the_write(observed, requested=REQUESTED_DISK)
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "worker.container_resource_bounding",
                    "requested": {"memory": REQUESTED_MEMORY, "disk": REQUESTED_DISK},
                    "observed": observed,
                },
                sort_keys=True,
            )
        )
    finally:
        _delete_app(APP_NAME, workspace)
    return 0


def _assert_cpu_request_is_a_floor(observed: dict[str, float], *, requested_cores: float) -> None:
    effective = observed["effective_cores"]
    if effective < MINIMUM_BURST_CORES:
        raise RuntimeError(
            f"a {requested_cores}-core request held the container to {effective} cores; "
            "the request is being applied as a ceiling rather than a floor"
        )


def _assert_memory_request_is_a_floor(
    observed: dict[str, float], *, requested: str, allocated_mib: int
) -> None:
    allocated = observed["allocated_mib"]
    if allocated != allocated_mib:
        raise RuntimeError(
            f"a {requested} request allowed only {allocated}MiB of {allocated_mib}MiB to be touched"
        )


def _assert_requested_disk_reached_the_container(
    observed: dict[str, float], *, requested: str, requested_bytes: int
) -> None:
    total = observed["root_total_bytes"]
    if total >= PLATFORM_DEFAULT_DISK_BYTES:
        raise RuntimeError(
            f"the container's root reports {total} bytes, the platform default; "
            f"the requested {requested} never reached the worker"
        )
    # Filesystem overhead leaves the visible total a little under the request.
    if not requested_bytes * 0.9 <= total <= requested_bytes:
        raise RuntimeError(
            f"the container's root reports {total} bytes, which is not the "
            f"requested {requested_bytes} bytes"
        )


def _assert_disk_ceiling_stopped_the_write(observed: dict[str, float], *, requested: str) -> None:
    written = observed["written_bytes"]
    total = observed["root_total_bytes"]
    write_errno = int(observed["write_errno"])
    if write_errno != errno.ENOSPC:
        raise RuntimeError(
            f"writing past the {requested} ceiling ended with errno {write_errno} "
            f"after {written} bytes; the ceiling did not reject the write"
        )
    if written > total:
        raise RuntimeError(f"the container wrote {written} bytes past a {total}-byte ceiling")
    # A rejection at the first chunk would mean something other than the ceiling
    # stopped the write.
    if written < total // 2:
        raise RuntimeError(
            f"the container was rejected after only {written} of {total} bytes, "
            "which is too early to be the requested ceiling"
        )


def _delete_app(name: str, workspace: str) -> None:
    client = resource_client(workspace=workspace, timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique resource bounding scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("resource bounding scenario app remained active after deletion")


if __name__ == "__main__":
    raise SystemExit(main())

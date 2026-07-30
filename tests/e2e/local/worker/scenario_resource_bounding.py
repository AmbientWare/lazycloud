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
        REQUESTED_DISK,
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
        _assert_cpu_request_is_a_floor(observed)
        _assert_memory_request_is_a_floor(observed)
        _assert_requested_disk_reached_the_container(observed)
        _assert_disk_ceiling_stopped_the_write(observed)
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


def _assert_cpu_request_is_a_floor(observed: dict[str, float]) -> None:
    from .workload_resource_bounding import MINIMUM_BURST_CORES, REQUESTED_CORES

    effective = observed["effective_cores"]
    if effective < MINIMUM_BURST_CORES:
        raise RuntimeError(
            f"a {REQUESTED_CORES}-core request held the container to {effective} cores; "
            "the request is being applied as a ceiling rather than a floor"
        )


def _assert_memory_request_is_a_floor(observed: dict[str, float]) -> None:
    from .workload_resource_bounding import ALLOCATE_MIB, REQUESTED_MEMORY

    allocated = observed["allocated_mib"]
    if allocated != ALLOCATE_MIB:
        raise RuntimeError(
            f"a {REQUESTED_MEMORY} request allowed only {allocated}MiB of "
            f"{ALLOCATE_MIB}MiB to be touched"
        )


def _assert_requested_disk_reached_the_container(observed: dict[str, float]) -> None:
    from .workload_resource_bounding import (
        DEFAULT_DISK_BYTES,
        REQUESTED_DISK,
        REQUESTED_DISK_BYTES,
    )

    total = observed["root_total_bytes"]
    if total >= DEFAULT_DISK_BYTES:
        raise RuntimeError(
            f"the container's root reports {total} bytes, the platform default; "
            f"the requested {REQUESTED_DISK} never reached the worker"
        )
    # Filesystem overhead leaves the visible total a little under the request.
    if not REQUESTED_DISK_BYTES * 0.9 <= total <= REQUESTED_DISK_BYTES:
        raise RuntimeError(
            f"the container's root reports {total} bytes, which is not the "
            f"requested {REQUESTED_DISK_BYTES} bytes"
        )


def _assert_disk_ceiling_stopped_the_write(observed: dict[str, float]) -> None:
    from .workload_resource_bounding import REQUESTED_DISK

    written = observed["written_bytes"]
    total = observed["root_total_bytes"]
    write_errno = int(observed["write_errno"])
    if write_errno != errno.ENOSPC:
        raise RuntimeError(
            f"writing past the {REQUESTED_DISK} ceiling ended with errno {write_errno} "
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

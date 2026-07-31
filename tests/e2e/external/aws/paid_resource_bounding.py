"""Prove container resource bounding holds on a real connected-AWS EC2 worker.

Invoking this explicitly named scenario authorizes its one bounded paid Function
on the existing warm baseline. It does not provision capacity: the machine is
owned by the readiness stage and returned by the cleanup stage.

The local stack proves the same behaviour against a Compose worker. This proves
it where the disk layout, free space, and loop-device availability are the
instance's own rather than a container host's. A container cannot start at all
unless its overlay was prepared — the spec builder refuses to fall back to the
shared image directory — so a result here is also proof that the loopback XFS
store provisioned and took a project quota on the instance.

Run from the repository root:

``uv run --env-file .env python -m tests.e2e.external.aws.paid_resource_bounding``
"""

from __future__ import annotations

import errno
import json
from collections.abc import Sequence

from lazycloud.cli.control import compute_client, resource_client
from shared.aws_connections import AwsAccountConnectionPhase
from tests.e2e.external import _support

# Eight busy processes against an eighth of a core. One whole core is eight times
# the request while staying far below the burst ceiling and the instance's four
# vCPUs, so the threshold does not depend on the instance type.
MINIMUM_BURST_CORES = 1.0
# The ceiling a container gets when nothing was requested.
PLATFORM_DEFAULT_DISK_BYTES = 100 * 1024**3


def main(argv: Sequence[str] | None = None) -> int:
    _ = argv
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

    compute = compute_client(timeout_seconds=30)
    connection = _support.first_public_call(compute.current_connection)
    if connection is None or connection.phase is not AwsAccountConnectionPhase.Ready:
        raise RuntimeError("the public AWS connection is not ready")
    machines = [
        item for item in compute.instances().data if item.provider == f"aws:{connection.id}"
    ]
    if not machines:
        raise RuntimeError("no connected AWS machine is available; run the readiness stage first")

    try:
        app.deploy()
        call = bounded_workload.spawn(
            processes=BURST_PROCESSES,
            burst_seconds=BURST_SECONDS,
            allocate_mib=ALLOCATE_MIB,
            chunk_mib=FILL_CHUNK_MIB,
        )
        observed = call.get(timeout_seconds=900, poll_interval_seconds=2)
        _assert_cpu_request_is_a_floor(observed, requested_cores=REQUESTED_CORES)
        _assert_memory_request_is_a_floor(
            observed, requested=REQUESTED_MEMORY, allocated_mib=ALLOCATE_MIB
        )
        _assert_requested_disk_reached_the_worker(
            observed, requested=REQUESTED_DISK, requested_bytes=REQUESTED_DISK_BYTES
        )
        _assert_disk_ceiling_stopped_the_write(observed, requested=REQUESTED_DISK)
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "capability": "aws.container_resource_bounding",
                    "instance": machines[0].id,
                    "instance_type": machines[0].instance_type,
                    "observed": observed,
                },
                sort_keys=True,
            )
        )
    finally:
        _delete_app(APP_NAME)
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


def _assert_requested_disk_reached_the_worker(
    observed: dict[str, float], *, requested: str, requested_bytes: int
) -> None:
    total = observed["root_total_bytes"]
    if total >= PLATFORM_DEFAULT_DISK_BYTES:
        raise RuntimeError(
            f"the container's root reports {total} bytes, the platform default; "
            f"the requested {requested} never reached the EC2 worker"
        )
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
    if written < total // 2:
        raise RuntimeError(
            f"the container was rejected after only {written} of {total} bytes, "
            "which is too early to be the requested ceiling"
        )


def _delete_app(name: str) -> None:
    client = resource_client(timeout_seconds=30)
    matches = [item for item in client.list_apps(active=True).data if item.name == name]
    if len(matches) > 1:
        raise RuntimeError("unique AWS bounding scenario app resolved more than once")
    if matches:
        client.delete_app(matches[0].id)
    if any(item.name == name for item in client.list_apps(active=True).data):
        raise RuntimeError("AWS bounding scenario app remained active after deletion")


if __name__ == "__main__":
    raise SystemExit(main())

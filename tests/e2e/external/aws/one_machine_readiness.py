"""Select connected AWS and prove the platform's one-machine warm baseline.

On success the stage emits the exact warm-baseline identity. On timeout it
still emits the observed public instance state as durable recovery evidence —
the paid capacity it requested stays owned by the account's compute
configuration until the cleanup stage restores zero.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from lazycloud.cli.control import compute_client
from shared.aws_connections import AwsAccountComputeConfiguration, AwsAccountConnectionPhase
from shared.compute_enrollment import MachineServiceState
from shared.http.aws_connections import AwsComputeConfigurationUpdateRequest
from shared.http.compute_policy import (
    WorkspaceComputeInstanceResponse,
    WorkspaceComputePolicyUpdateRequest,
)
from tests.e2e.external import _support


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    deadline = _support.Deadline(args.timeout)

    client = compute_client(timeout_seconds=30)
    try:
        connection = _support.first_public_call(client.current_connection)
    except _support.MissingPrerequisite as exc:
        return _support.skip("AWS readiness", exc)
    if connection is None or connection.phase is not AwsAccountConnectionPhase.Ready:
        raise RuntimeError("the public AWS connection is not ready")

    policy = client.policy()
    if policy.default_pool != "aws":
        client.update_policy(
            WorkspaceComputePolicyUpdateRequest(
                expected_revision=policy.revision,
                default_pool="aws",
            )
        )
    current = connection.compute
    parity = AwsAccountComputeConfiguration(
        revision=current.revision,
        default_region=current.default_region,
        default_instance_type=current.default_instance_type,
        initial_cpu_workers=1,
        min_cpu_workers=1,
        max_cpu_instances=(
            None if current.max_cpu_instances is None else max(1, current.max_cpu_instances)
        ),
        max_gpu_instances=current.max_gpu_instances,
        min_free_cpu_millicores=1_000,
        min_free_memory_mib=1_024,
        allowed_regions=current.allowed_regions,
        allowed_instance_types=current.allowed_instance_types,
        idle_timeout_seconds=current.idle_timeout_seconds,
        root_volume_gib=current.root_volume_gib,
    )
    if current != parity:
        client.update_compute_configuration(
            AwsComputeConfigurationUpdateRequest(
                expected_revision=current.revision,
                compute=parity,
            )
        )

    last_observed: list[WorkspaceComputeInstanceResponse] = []

    def check() -> WorkspaceComputeInstanceResponse | None:
        summary = client.summary()
        connected = [
            item for item in client.instances().data if item.provider == f"aws:{connection.id}"
        ]
        last_observed[:] = connected
        ready = [
            item
            for item in connected
            if item.service_state is MachineServiceState.Serving and item.machine_id is not None
        ]
        if (
            summary.instances.total == 1
            and summary.instances.ready == 1
            and summary.instances.pending == 0
            and summary.instances.degraded == 0
            and len(connected) == 1
            and len(ready) == 1
            and summary.cost.hourly_micros > 0
        ):
            return ready[0]
        return None

    try:
        machine = _support.poll_until(deadline, "the one-machine AWS warm baseline", check)
    except RuntimeError:
        observed: list[dict[str, object]] = [
            {
                "bootstrap_phase": item.bootstrap_phase,
                "instance_id": item.id,
                "machine_id": item.machine_id,
                "region": item.region,
                "status": item.status,
            }
            for item in last_observed
        ]
        _support.emit_evidence(
            {
                "capability": "compute.aws.warm_baseline",
                "connection_id": connection.id,
                "observed_instances": observed,
                "ready": 0,
                "recovery": "run tests.e2e.external.aws.cleanup to restore zero capacity",
            }
        )
        raise

    summary = client.summary()
    _support.emit_evidence(
        {
            "capability": "compute.aws.warm_baseline",
            "connection_id": connection.id,
            "hourly_micros": summary.cost.hourly_micros,
            "instance_id": machine.id,
            "instance_type": machine.instance_type,
            "machine_id": machine.machine_id,
            "ready": 1,
            "region": machine.region,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Select connected AWS and prove the managed policy's one-machine warm baseline.

The cleanup stage disconnects the account and removes its managed capacity.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from lazycloud.cli.control import compute_client
from shared.aws_connections import AwsAccountConnectionPhase
from shared.compute_fleet import MachineLifecycle
from shared.http.compute_policy import (
    ConnectionMachineResponse,
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

    last_observed: list[ConnectionMachineResponse] = []

    def check() -> ConnectionMachineResponse | None:
        summary = client.summary()
        connected = [
            item for item in client.instances().data if item.provider == f"aws:{connection.id}"
        ]
        last_observed[:] = connected
        instance_signals: list[dict[str, str]] = [
            {"id": item.id, "lifecycle": item.lifecycle.value, "message": item.lifecycle_message}
            for item in connected
        ]
        _support.emit_evidence(
            {
                "connection_id": connection.id,
                "instances": instance_signals,
                "ready": summary.instances.ready,
                "pending": summary.instances.pending,
                "degraded": summary.instances.degraded,
            }
        )
        ready = [
            item
            for item in connected
            if item.lifecycle is MachineLifecycle.Ready and item.connected
        ]
        if (
            summary.instances.total == 1
            and summary.instances.ready == 1
            and summary.instances.pending == 0
            and summary.instances.degraded == 0
            and len(connected) == 1
            and len(ready) == 1
        ):
            return ready[0]
        return None

    try:
        machine = _support.poll_until(deadline, "the one-machine AWS warm baseline", check)
    except RuntimeError:
        observed: list[dict[str, object]] = [
            {
                "lifecycle": item.lifecycle.value,
                "instance_id": item.instance_id,
                "machine_id": item.id,
                "region": item.region,
                "connected": item.connected,
            }
            for item in last_observed
        ]
        _support.emit_evidence(
            {
                "capability": "compute.aws.warm_baseline",
                "connection_id": connection.id,
                "observed_instances": observed,
                "ready": 0,
                "recovery": "run tests.e2e.external.aws.cleanup to disconnect the account",
            }
        )
        raise

    summary = client.summary()
    _support.emit_evidence(
        {
            "capability": "compute.aws.warm_baseline",
            "connection_id": connection.id,
            "hourly_micros": summary.cost.hourly_micros,
            "instance_id": machine.instance_id,
            "instance_type": machine.instance_type,
            "machine_id": machine.id,
            "ready": 1,
            "region": machine.region,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

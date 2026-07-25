"""Prove the prepared local platform has an agent-managed schedulable worker."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence

from lazycloud.cli.control import compute_client, control_config, resource_client
from shared.compute_enrollment import MachineReadinessPhase
from shared.http.errors import HttpApiError, HttpTransportError
from shared.scheduling import SchedulerWorkerStatus

SKIP = 77


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)

    try:
        control_config(timeout_seconds=30)
        client = resource_client(timeout_seconds=30)
        compute = compute_client(timeout_seconds=30)
        client.list_pools()
    except HttpTransportError as exc:
        print(f"local Compose prerequisite unavailable: {exc}", file=sys.stderr)
        return SKIP
    except HttpApiError as exc:
        if exc.status_code != 401:
            raise
        print("local Compose prerequisite unavailable: authentication failed", file=sys.stderr)
        return SKIP

    deadline = time.monotonic() + args.timeout
    while True:
        pools = client.list_pools().pools
        machines = compute.list_pool_machines("default").data
        workers = client.list_workers().workers
        default_pool = next((item for item in pools if item.name == "default"), None)
        ready_machines = [
            item
            for item in machines
            if item.pool_name == "default"
            and item.provider_name == "agent"
            and item.readiness_phase is MachineReadinessPhase.Ready
            and item.schedulable
        ]
        ready_machine_ids = {item.id for item in ready_machines}
        ready_workers = [
            item
            for item in workers
            if item.pool_name == "default"
            and item.machine_id in ready_machine_ids
            and item.status == SchedulerWorkerStatus.Available.value
        ]
        if (
            default_pool is not None
            and default_pool.provider == "agent"
            and ready_machines
            and ready_workers
        ):
            print(
                json.dumps(
                    {
                        "capability": "compose.agent_managed_worker",
                        "machine_ids": sorted(ready_machine_ids),
                        "pool": default_pool.name,
                        "worker_ids": sorted(item.id for item in ready_workers),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting for the public agent-managed worker")
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify one explicitly named prepared agent joined its Tailnet-backed pool."""

from __future__ import annotations

import argparse
import json

from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.resource.control import ResourceControlClient
from shared.scheduling import SchedulerWorkerStatus
from tests.e2e.external import _support


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-name", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _support.skip("Tailnet agent-join", "--live is required")
    try:
        endpoint, token, workspace = _support.prepared_gateway(
            token_variable="LAZYCLOUD_E2E_ADMIN_TOKEN"
        )
    except _support.MissingPrerequisite as exc:
        return _support.skip("Tailnet agent-join", exc)
    resources = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    compute = ComputeClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    pools = [pool for pool in resources.list_pools().pools if pool.name == args.pool_name]
    if len(pools) != 1 or pools[0].labels.get("transport") != "tsnet_restricted":
        raise RuntimeError("guarded pool is not the prepared Tailnet-backed pool")
    workers = [worker for worker in resources.list_workers().workers if worker.id == args.worker_id]
    if len(workers) != 1:
        raise RuntimeError("guarded Tailnet worker is not publicly observable")
    worker = workers[0]
    if worker.machine_id != args.machine_id or worker.pool_name != args.pool_name:
        raise RuntimeError("Tailnet worker is attached to the wrong machine or pool")
    if worker.status != SchedulerWorkerStatus.Available.value:
        raise RuntimeError(f"Tailnet worker is not available: {worker.status}")
    machines = compute.list_pool_machines(args.pool_name, limit=100).data
    matches = [machine for machine in machines if machine.id == args.machine_id]
    if len(matches) != 1:
        raise RuntimeError("Tailnet pool does not retain the guarded machine identity")
    print(
        json.dumps(
            {
                "accepted": True,
                "pool": args.pool_name,
                "worker_id": worker.id,
                "machine_id": matches[0].id,
                "worker_status": worker.status,
                "cleanup": "read-only; prepared agent and pool retained",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prove billing attribution for an existing real Function task."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta

import httpx
from lazycloud.clients.observability.control import ObservabilityControlClient
from shared.http.usage import (
    UsageBillingOverviewResponse,
    UsageBillingWorkloadListResponse,
)
from shared.usage import UsageMetric

BLOCKED = 77


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("billing acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    headers = {"Authorization": f"Bearer {token}"}
    usage = ObservabilityControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    task_records = usage.usage_records(
        metric=UsageMetric.TaskCount,
        resource_type="task",
        resource_id=args.task_id,
    ).data
    if not any(record.quantity > 0 for record in task_records):
        raise RuntimeError("the supplied task has no real task-count usage evidence")

    now = datetime.now(UTC)
    params = {
        "workspace": workspace,
        "start": (now - timedelta(days=1)).isoformat(),
        "end": (now + timedelta(minutes=1)).isoformat(),
    }
    overview_response = httpx.get(
        f"{endpoint}/api/v1/usage/billing",
        params=params,
        headers=headers,
        timeout=30,
    )
    overview_response.raise_for_status()
    overview = UsageBillingOverviewResponse.model_validate(overview_response.json())
    matching_apps = [item for item in overview.apps if item.app_id == args.app_id]
    if len(matching_apps) != 1 or matching_apps[0].tasks < 1:
        raise RuntimeError("billing overview omitted the supplied task's app attribution")

    workload_response = httpx.get(
        f"{endpoint}/api/v1/usage/billing/workloads",
        params={**params, "app_id": args.app_id},
        headers=headers,
        timeout=30,
    )
    workload_response.raise_for_status()
    workloads = UsageBillingWorkloadListResponse.model_validate(workload_response.json())
    attributed = [item for item in workloads.data if item.tasks > 0]
    if not attributed:
        raise RuntimeError("billing workload report omitted task attribution")
    print(
        json.dumps(
            {
                "accepted": True,
                "app_id": args.app_id,
                "task_id": args.task_id,
                "app_tasks": matching_apps[0].tasks,
                "attributed_workloads": len(attributed),
                "currency": overview.currency,
                "cleanup": "read-only; no resources created",
            },
            sort_keys=True,
        )
    )
    return 0


def _prerequisites() -> tuple[str, str, str]:
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    token = os.getenv("LAZYCLOUD_TOKEN", "")
    workspace = os.getenv("LAZYCLOUD_WORKSPACE", "default")
    if not endpoint or not token:
        raise SystemExit(_blocked("LAZYCLOUD_ENDPOINT and LAZYCLOUD_TOKEN are required"))
    try:
        httpx.get(f"{endpoint}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        raise SystemExit(_blocked(f"prepared control plane is unavailable: {exc}")) from exc
    return endpoint, token, workspace


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

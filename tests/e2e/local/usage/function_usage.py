"""Prove one public Function task produces durable public usage evidence."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from lazycloud.clients.observability.control import ObservabilityControlClient
from lazycloud.clients.resource.control import ResourceControlClient
from shared.usage import UsageMetric

from lazycloud import App

BLOCKED = 77
APP_NAME = f"e2e_usage_function_{uuid4().hex[:8]}"
app = App(APP_NAME)


@app.function(name="usage-function")
def usage_function(value: int) -> dict[str, int]:
    return {"value": value * 2}


def main() -> int:
    if "--live" not in sys.argv:
        return _blocked("Function usage acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    resources = ResourceControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    usage = ObservabilityControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    started_at = datetime.now(UTC)
    app_id = ""
    task_id = ""
    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    try:
        call = usage_function.spawn(21)
        task_id = call.task_id
        result = call.get(timeout_seconds=180, poll_interval_seconds=0.25)
        if result != {"value": 42}:
            raise RuntimeError("Function returned an unexpected result")
        app_id = _owned_app(resources).id
        record = _await_task_usage(usage, task_id, started_at)
        evidence = {
            "app_id": app_id,
            "task_id": task_id,
            "usage_record_id": record.id,
            "metric": record.metric.value,
            "quantity": record.quantity,
            "started_at": started_at.isoformat(),
        }
    except BaseException as exc:
        primary_error = exc
    cleanup_error = _delete_app(resources)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"Function usage failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"Function usage cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "evidence": evidence,
                "cleanup": "owned app deleted",
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


def _owned_app(client: ResourceControlClient):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        matches = [item for item in client.list_apps().data if item.name == APP_NAME]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"multiple apps matched unique name {APP_NAME}")
        time.sleep(0.25)
    raise RuntimeError(f"app {APP_NAME} was not publicly observable")


def _await_task_usage(
    client: ObservabilityControlClient,
    task_id: str,
    started_at: datetime,
):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        records = client.usage_records(
            metric=UsageMetric.TaskCount,
            resource_type="task",
            resource_id=task_id,
            start=started_at,
        ).data
        positive = [record for record in records if record.quantity > 0]
        if len(positive) == 1:
            return positive[0]
        if len(positive) > 1:
            raise RuntimeError("Function task produced duplicate task-count usage")
        time.sleep(1)
    raise RuntimeError("Function task usage did not become publicly observable")


def _delete_app(client: ResourceControlClient) -> str:
    try:
        matches = [item for item in client.list_apps().data if item.name == APP_NAME]
        if len(matches) > 1:
            return f"multiple apps matched unique name {APP_NAME}"
        if matches:
            client.delete_app(matches[0].id)
        if any(item.name == APP_NAME for item in client.list_apps().data):
            return f"app {APP_NAME} remains after public deletion"
    except Exception as exc:
        return str(exc)
    return ""


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

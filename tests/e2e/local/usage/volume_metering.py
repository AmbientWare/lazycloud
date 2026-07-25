"""Prove one uniquely named Volume produces public byte-time usage."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from lazycloud.clients.observability.control import ObservabilityControlClient
from shared.usage import UsageMetric

from lazycloud import Volume

BLOCKED = 77


def main() -> int:
    if "--live" not in sys.argv:
        return _blocked("Volume metering acceptance requires --live")
    endpoint, token, workspace = _prerequisites()
    name = f"e2e-meter-{uuid4().hex[:10]}"
    volume = Volume(name, workspace=workspace)
    usage = ObservabilityControlClient.from_endpoint(
        endpoint,
        token=token,
        workspace=workspace,
    )
    started_at = datetime.now(UTC)
    created = False
    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    try:
        instance = volume.create()
        created = True
        volume.write_text("metered.txt", "lazycloud-volume-metering\n")
        record = _await_volume_usage(usage, instance.id, started_at)
        evidence = {
            "volume_id": instance.id,
            "usage_record_id": record.id,
            "metric": record.metric.value,
            "quantity": record.quantity,
            "unit": record.unit.value,
        }
    except BaseException as exc:
        primary_error = exc
    cleanup_error = ""
    if created:
        try:
            volume.delete()
        except Exception as exc:
            cleanup_error = str(exc)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"Volume metering failed and cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"Volume cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "evidence": evidence,
                "cleanup": f"owned volume {name} deleted",
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


def _await_volume_usage(
    client: ObservabilityControlClient,
    volume_id: str,
    started_at: datetime,
):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        records = client.usage_records(
            metric=UsageMetric.PersistentVolumeByteSeconds,
            resource_type="volume",
            resource_id=volume_id,
            start=started_at,
        ).data
        positive = [record for record in records if record.quantity > 0]
        if positive:
            return positive[-1]
        time.sleep(1)
    raise RuntimeError("Volume byte-time usage did not become publicly observable")


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

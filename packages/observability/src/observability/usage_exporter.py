from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import JsonValue, SecretStr
from shared.usage import (
    UsageCollectorKind,
    UsageMetricEmissionPlan,
    UsageMetricHeaderValue,
    UsageMetricOperation,
    UsageMetricsSinkSettings,
    UsageRecord,
    UsageUnit,
    plan_usage_metric_emission,
    usage_metric_from_name,
    usage_to_prometheus,
)

DEFAULT_PROMETHEUS_EXPORT_RECORD_LIMIT = 5000


class UsageMetricHttpPoster(Protocol):
    def __call__(
        self,
        target: str,
        body: dict[str, JsonValue],
        headers: dict[str, str],
    ) -> None: ...


class UsageMetricsExporter:
    def __init__(
        self,
        settings: UsageMetricsSinkSettings,
        *,
        http_post: UsageMetricHttpPoster | None = None,
        max_prometheus_records: int = DEFAULT_PROMETHEUS_EXPORT_RECORD_LIMIT,
    ) -> None:
        self.settings = settings
        self.http_post = http_post or _urllib_post
        self._prometheus_records: deque[UsageMetricEmissionPlan] = deque(
            maxlen=max(max_prometheus_records, 1)
        )

    def emit(
        self,
        *,
        name: str,
        metadata: Mapping[str, JsonValue] | None = None,
        value: float = 1.0,
        operation: UsageMetricOperation = UsageMetricOperation.IncrementCounter,
    ) -> UsageMetricEmissionPlan | None:
        plan = plan_usage_metric_emission(
            self.settings,
            name=name,
            metadata=metadata,
            value=value,
            operation=operation,
        )
        if plan is None:
            return None
        if plan.collector is UsageCollectorKind.Prometheus:
            self._prometheus_records.append(plan)
            return plan
        if plan.collector is UsageCollectorKind.OpenMeter:
            if plan.target is None:
                raise ValueError("openmeter usage target is required")
            self.http_post(plan.target, plan.body, _http_headers(plan.headers))
            return plan
        raise ValueError(f"unsupported usage collector: {plan.collector}")

    def prometheus_text(self) -> str:
        records: list[UsageRecord] = []
        for plan in self._prometheus_records:
            labels = {
                str(key): str(value) for key, value in plan.metadata.items() if value is not None
            }
            records.append(
                UsageRecord(
                    id=str(uuid4()),
                    workspace_id=str(plan.metadata.get("workspace_id") or plan.source),
                    resource_type=str(plan.metadata.get("resource_type") or "usage_metric"),
                    resource_id=str(plan.metadata.get("resource_id") or plan.name),
                    metric=usage_metric_from_name(plan.name),
                    quantity=plan.value,
                    unit=UsageUnit.Count,
                    labels=labels,
                )
            )
        return usage_to_prometheus(records)


def _urllib_post(
    target: str,
    body: dict[str, JsonValue],
    headers: dict[str, str],
) -> None:
    request = Request(
        target,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response.read(1 << 20)
    except HTTPError as exc:
        detail = exc.read(1 << 20).decode("utf-8", errors="replace")
        raise RuntimeError(f"usage metrics export failed with status {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"usage metrics export failed: {exc.reason}") from exc


def _http_headers(headers: Mapping[str, UsageMetricHeaderValue]) -> dict[str, str]:
    return {
        key: value.get_secret_value() if isinstance(value, SecretStr) else value
        for key, value in headers.items()
    }


__all__ = [
    "DEFAULT_PROMETHEUS_EXPORT_RECORD_LIMIT",
    "UsageMetricHttpPoster",
    "UsageMetricsExporter",
]

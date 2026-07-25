from __future__ import annotations

from observability.usage_exporter import UsageMetricsExporter
from pydantic import JsonValue, SecretStr
from shared.usage import (
    UsageCollectorKind,
    UsageMetric,
    UsageMetricsSinkSettings,
)


def test_usage_exporter_masks_credentials_and_preserves_post_contract() -> None:
    posted: list[tuple[str, dict[str, JsonValue], dict[str, str]]] = []
    settings = UsageMetricsSinkSettings(
        collector=UsageCollectorKind.OpenMeter,
        openmeter_url="https://meter.example.test/events",
        openmeter_api_key=SecretStr("sentinel-meter-secret"),
    )
    exporter = UsageMetricsExporter(
        settings,
        http_post=lambda target, body, headers: posted.append((target, body, headers)),
    )

    plan = exporter.emit(
        name=UsageMetric.TaskCount.value,
        metadata={"workspace_id": "workspace-1", "nested": {"attempt": 1}},
    )

    assert plan is not None
    assert posted[0][1]["data"] == {
        "workspace_id": "workspace-1",
        "nested": {"attempt": 1},
        "value": 1.0,
    }
    assert posted[0][2]["Authorization"] == "Bearer sentinel-meter-secret"
    assert "sentinel-meter-secret" not in repr(settings)
    assert "sentinel-meter-secret" not in repr(plan)

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from shared.http.observability import (
    ContainerMetricsPointResponse,
    ContainerMetricsTimeseriesResponse,
)
from shared.realtime.contracts import ContainerMetricsPayload

from observability.stream_state import RedisStreamRecord


def container_metrics_timeseries(
    container_id: str,
    records: Iterable[RedisStreamRecord],
) -> ContainerMetricsTimeseriesResponse:
    """Map container.metrics stream records to a chronological typed timeseries."""
    points: list[ContainerMetricsPointResponse] = []
    for record in records:
        data = record.body.get("data")
        raw_time = record.body.get("time")
        if not isinstance(data, Mapping) or not isinstance(raw_time, str):
            continue
        payload = ContainerMetricsPayload.model_validate(data)
        metrics = payload.metrics
        points.append(
            ContainerMetricsPointResponse(
                timestamp=datetime.fromisoformat(raw_time),
                sample_interval_ms=metrics.sample_interval_ms,
                cpu_millicores=metrics.cpu_used,
                cpu_total_millicores=metrics.cpu_total,
                cpu_pct=metrics.cpu_pct,
                memory_rss_bytes=metrics.memory_rss_bytes,
                memory_total_bytes=metrics.memory_total_bytes,
                network_recv_bytes=metrics.network_recv_bytes,
                network_sent_bytes=metrics.network_sent_bytes,
                disk_read_bytes=metrics.disk_read_bytes,
                disk_write_bytes=metrics.disk_write_bytes,
                gpu_memory_used_bytes=metrics.gpu_memory_used_bytes,
                gpu_memory_total_bytes=metrics.gpu_memory_total_bytes,
                gpu_type=metrics.gpu_type,
            )
        )
    points.sort(key=lambda point: point.timestamp)
    return ContainerMetricsTimeseriesResponse(container_id=container_id, points=tuple(points))

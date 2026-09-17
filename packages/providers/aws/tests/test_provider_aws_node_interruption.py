from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from provider_aws import (
    AwsEc2SpotInterruptionMonitor,
    AwsInstanceMetadataResponse,
    AwsSpotInterruptionAction,
    AwsSpotInterruptionMonitorError,
)
from shared.compute_enrollment import CapacitySignalKind


@dataclass(slots=True)
class _MetadataTransport:
    responses: list[AwsInstanceMetadataResponse]
    requests: list[tuple[str, str, dict[str, str], int]] = field(default_factory=list)

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AwsInstanceMetadataResponse:
        del timeout_seconds
        self.requests.append((method, path, headers, max_response_bytes))
        return self.responses.pop(0)


def test_spot_interruption_monitor_reports_no_risk_when_metadata_is_absent() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(status_code=404, body=b""),
            AwsInstanceMetadataResponse(status_code=404, body=b""),
        ]
    )
    monitor = AwsEc2SpotInterruptionMonitor(transport=metadata, monotonic=lambda: 10.0)

    assert monitor.poll() is None


def test_rebalance_recommendation_has_no_termination_deadline() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(status_code=404, body=b""),
            AwsInstanceMetadataResponse(
                status_code=200, body=b'{"noticeTime":"2026-09-16T21:02:43Z"}'
            ),
        ]
    )
    notice = AwsEc2SpotInterruptionMonitor(transport=metadata).poll()
    assert notice is not None
    assert notice.kind is CapacitySignalKind.Rebalance
    assert notice.notice_at is None
    assert notice.observed_at == datetime(2026, 9, 16, 21, 2, 43, tzinfo=UTC)


def test_spot_interruption_monitor_refreshes_rejected_token_and_parses_notice() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"expired-session"),
            AwsInstanceMetadataResponse(status_code=401, body=b""),
            AwsInstanceMetadataResponse(status_code=200, body=b"current-session"),
            AwsInstanceMetadataResponse(
                status_code=200,
                body=b'{"action":"terminate","time":"2026-07-21T15:30:00Z"}',
            ),
        ]
    )
    monitor = AwsEc2SpotInterruptionMonitor(transport=metadata, monotonic=lambda: 10.0)

    notice = monitor.poll()

    assert notice is not None
    assert notice.action is AwsSpotInterruptionAction.Terminate
    assert notice.notice_at == datetime(2026, 7, 21, 15, 30, tzinfo=UTC)
    assert metadata.requests[-1][2] == {"X-aws-ec2-metadata-token": "current-session"}


def test_spot_interruption_monitor_rejects_invalid_action_payload() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(
                status_code=200,
                body=b'{"action":"reboot","time":"2026-07-21T15:30:00Z"}',
            ),
        ]
    )

    with pytest.raises(AwsSpotInterruptionMonitorError, match="invalid action"):
        AwsEc2SpotInterruptionMonitor(transport=metadata).poll()

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from provider_aws import (
    AWS_EC2_SPOT_INSTANCE_ACTION_PATH,
    AwsEc2SpotInterruptionMonitor,
    AwsInstanceMetadataResponse,
    AwsSpotInterruptionAction,
    AwsSpotInterruptionMonitorError,
)


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


def test_spot_interruption_monitor_reuses_imdsv2_token_when_no_notice_exists() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(status_code=404, body=b""),
            AwsInstanceMetadataResponse(status_code=404, body=b""),
        ]
    )
    monitor = AwsEc2SpotInterruptionMonitor(transport=metadata, monotonic=lambda: 10.0)

    assert monitor.poll() is None
    assert monitor.poll() is None

    assert [request[:2] for request in metadata.requests] == [
        ("PUT", "/latest/api/token"),
        ("GET", AWS_EC2_SPOT_INSTANCE_ACTION_PATH),
        ("GET", AWS_EC2_SPOT_INSTANCE_ACTION_PATH),
    ]
    assert metadata.requests[1][2] == {"X-aws-ec2-metadata-token": "imds-session"}


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

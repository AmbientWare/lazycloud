from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator
from shared.compute_enrollment import CapacitySignalKind
from shared.timestamps import utc_now

from .provider_node_proof import (
    AWS_IMDS_MAX_TEXT_BYTES,
    AWS_IMDS_TIMEOUT_SECONDS,
    AWS_IMDS_TOKEN_TTL_SECONDS,
    AwsDirectInstanceMetadataTransport,
    AwsInstanceMetadataResponse,
    AwsInstanceMetadataTransport,
    AwsProviderNodeProofError,
)

AWS_EC2_SPOT_INSTANCE_ACTION_PATH = "/latest/meta-data/spot/instance-action"
AWS_EC2_REBALANCE_PATH = "/latest/meta-data/events/recommendations/rebalance"
AWS_IMDS_TOKEN_PATH = "/latest/api/token"
AWS_IMDS_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class AwsSpotInterruptionMonitorError(RuntimeError):
    """Raised when the EC2 interruption sentinel receives invalid metadata."""


class AwsSpotInterruptionAction(StrEnum):
    Hibernate = "hibernate"
    Stop = "stop"
    Terminate = "terminate"


class _AwsSpotInstanceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: AwsSpotInterruptionAction
    time: datetime

    @field_validator("time")
    @classmethod
    def notice_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("EC2 Spot interruption time must include a timezone")
        return value


@dataclass(frozen=True, slots=True)
class AwsSpotInterruptionNotice:
    kind: CapacitySignalKind
    observed_at: datetime
    action: AwsSpotInterruptionAction | None = None
    notice_at: datetime | None = None


class _AwsRebalanceRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    noticeTime: datetime

    @field_validator("noticeTime")
    @classmethod
    def time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("EC2 rebalance time must include a timezone")
        return value


@dataclass(slots=True)
class AwsEc2SpotInterruptionMonitor:
    transport: AwsInstanceMetadataTransport = field(
        default_factory=AwsDirectInstanceMetadataTransport
    )
    timeout_seconds: float = AWS_IMDS_TIMEOUT_SECONDS
    monotonic: Callable[[], float] = time.monotonic
    _token: str = field(default="", init=False, repr=False)
    _token_refresh_at: float = field(default=0.0, init=False, repr=False)

    def poll(self) -> AwsSpotInterruptionNotice | None:
        response = self._request(AWS_EC2_SPOT_INSTANCE_ACTION_PATH)
        if response.status_code == 404:
            return self._rebalance_recommendation()
        if response.status_code != 200:
            raise AwsSpotInterruptionMonitorError(
                f"EC2 Spot interruption metadata failed with status {response.status_code}"
            )
        try:
            action = _AwsSpotInstanceAction.model_validate_json(response.body)
        except ValueError as exc:
            raise AwsSpotInterruptionMonitorError(
                "EC2 Spot interruption metadata returned an invalid action"
            ) from exc
        return AwsSpotInterruptionNotice(
            kind=CapacitySignalKind.Interruption,
            observed_at=utc_now(),
            action=action.action,
            notice_at=action.time
            if action.action is not AwsSpotInterruptionAction.Hibernate
            else None,
        )

    def _request(self, path: str) -> AwsInstanceMetadataResponse:
        response = self._request_metadata(path)
        if response.status_code == 401:
            self._token = ""
            self._token_refresh_at = 0.0
            response = self._request_metadata(path)
        return response

    def _rebalance_recommendation(self) -> AwsSpotInterruptionNotice | None:
        response = self._request(AWS_EC2_REBALANCE_PATH)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AwsSpotInterruptionMonitorError(
                f"EC2 rebalance metadata failed with status {response.status_code}"
            )
        try:
            recommendation = _AwsRebalanceRecommendation.model_validate_json(response.body)
        except ValueError as exc:
            raise AwsSpotInterruptionMonitorError(
                "EC2 rebalance metadata returned an invalid recommendation"
            ) from exc
        return AwsSpotInterruptionNotice(
            kind=CapacitySignalKind.Rebalance,
            observed_at=recommendation.noticeTime,
        )

    def _request_metadata(self, path: str) -> AwsInstanceMetadataResponse:
        token = self._metadata_token()
        try:
            return self.transport.request(
                method="GET",
                path=path,
                headers={"X-aws-ec2-metadata-token": token},
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=AWS_IMDS_MAX_TEXT_BYTES,
            )
        except AwsProviderNodeProofError as exc:
            raise AwsSpotInterruptionMonitorError(
                "EC2 Spot interruption metadata is unavailable"
            ) from exc

    def _metadata_token(self) -> str:
        now = self.monotonic()
        if self._token and now < self._token_refresh_at:
            return self._token
        try:
            response = self.transport.request(
                method="PUT",
                path=AWS_IMDS_TOKEN_PATH,
                headers={"X-aws-ec2-metadata-token-ttl-seconds": str(AWS_IMDS_TOKEN_TTL_SECONDS)},
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=AWS_IMDS_MAX_TEXT_BYTES,
            )
        except AwsProviderNodeProofError as exc:
            raise AwsSpotInterruptionMonitorError("EC2 metadata token is unavailable") from exc
        if response.status_code != 200:
            raise AwsSpotInterruptionMonitorError(
                f"EC2 metadata token request failed with status {response.status_code}"
            )
        try:
            token = response.body.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise AwsSpotInterruptionMonitorError(
                "EC2 metadata token response is not valid text"
            ) from exc
        if not token:
            raise AwsSpotInterruptionMonitorError("EC2 metadata returned an empty token")
        self._token = token
        self._token_refresh_at = now + (
            AWS_IMDS_TOKEN_TTL_SECONDS - AWS_IMDS_TOKEN_REFRESH_MARGIN_SECONDS
        )
        return token


__all__ = [
    "AWS_EC2_SPOT_INSTANCE_ACTION_PATH",
    "AwsEc2SpotInterruptionMonitor",
    "AwsSpotInterruptionAction",
    "AwsSpotInterruptionMonitorError",
    "AwsSpotInterruptionNotice",
]

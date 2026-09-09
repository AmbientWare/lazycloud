from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
)

from observability.usage import UsageService


@dataclass(frozen=True, slots=True)
class TransferAttribution:
    workspace_id: str
    resource_type: str
    resource_id: str
    stub_id: str = ""


@dataclass(slots=True)
class OutboundTransferMeter:
    """Meter payload bytes accepted by the public transport, excluding headers.

    Flush during long streams. An uncertain database acknowledgement retains
    the exact record for the final retry, including its identity and timestamps.
    A process crash can lose the unflushed tail.
    """

    usage: UsageService
    attribution: TransferAttribution
    transport: str
    billable: bool
    transfer_id: str = field(default_factory=lambda: str(uuid4()))
    _started_at: datetime = field(default_factory=utc_now)
    _bytes: int = 0
    _sequence: int = 0
    _pending: UsageRecord | None = None

    async def sent(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("sent byte count cannot be negative")
        self._bytes += byte_count
        if self._bytes >= 1024 * 1024 or utc_now() - self._started_at >= timedelta(seconds=1):
            await self.flush()

    async def flush(self) -> None:
        if not self._bytes:
            return
        if self._pending is None:
            ended_at = max(utc_now(), self._started_at + timedelta(microseconds=1))
            self._pending = UsageRecord(
                id=usage_record_id("public-transfer", self.transfer_id, self._sequence),
                workspace_id=self.attribution.workspace_id,
                resource_type=self.attribution.resource_type,
                resource_id=self.attribution.resource_id,
                metric=UsageMetric.NetworkEgressBytes
                if self.billable
                else UsageMetric.NetworkSentBytes,
                quantity=self._bytes,
                unit=UsageUnit.Bytes,
                labels={"stub_id": self.attribution.stub_id, "transport": self.transport},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: self._started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
                    "measurement": "public_response_payload_bytes",
                    "internet_destination_verified": self.billable,
                },
                created_at=ended_at,
            )
        await self.usage.append_async(self._pending)
        self._started_at = self._pending.created_at
        self._bytes -= int(self._pending.quantity)
        self._sequence += 1
        self._pending = None

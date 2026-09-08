from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from database.repositories.identity import WorkspaceRepository
from database.repositories.observability import UsageRepository
from database.repositories.storage import VolumeMeteringTarget, VolumeRepository
from observability.usage_pricing import MeteredUsagePricer
from pydantic import JsonValue
from shared.timestamps import to_utc, utc_now
from shared.usage import (
    METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY,
    METERING_OBSERVATION_QUALITY_METADATA_KEY,
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    MeteringObservationQuality,
    UsageMetric,
    UsageRecord,
    UsageUnit,
    usage_record_id,
)

from storage.artifact_metering import meter_due_artifacts
from storage.context import StorageContext
from storage.volume_filesystem import VolumeFilesystem, VolumeNamespace

LOGGER = logging.getLogger(__name__)

_MILLISECOND = timedelta(milliseconds=1)
_MILLISECONDS_PER_SECOND = Decimal(1_000)


@dataclass(frozen=True, slots=True)
class PersistentVolumeMeteringResult:
    workspace_id: str
    volume_name: str
    previous_size_bytes: int
    observed_size_bytes: int | None
    window_started_at: datetime
    window_ended_at: datetime
    byte_seconds: Decimal
    """Bytes held times the seconds they were held for, exactly.

    A terabyte-scale volume over an hour is already past the range a binary float
    holds every integer in, and this is the quantity the ledger prices from.
    """

    usage_record: UsageRecord


@dataclass(frozen=True, slots=True)
class PersistentVolumeMeteringBatch:
    metered_count: int
    failure_count: int


@dataclass(slots=True)
class PersistentVolumeMeteringService:
    context: StorageContext
    filesystem: VolumeFilesystem
    interval: timedelta = timedelta(seconds=60)

    @classmethod
    def from_settings(
        cls,
        context: StorageContext,
        *,
        filesystem: VolumeFilesystem,
        interval_seconds: float = 60,
    ) -> PersistentVolumeMeteringService:
        if interval_seconds <= 0:
            raise ValueError("volume metering interval must be positive")
        return cls(
            context=context,
            filesystem=filesystem,
            interval=timedelta(seconds=interval_seconds),
        )

    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> PersistentVolumeMeteringBatch:
        observed_at = to_utc(now or utc_now())
        with self.context.database.session() as session:
            targets = VolumeRepository(session).list_metering_targets(
                metered_before=observed_at - self.interval,
                limit=limit,
            )
        metered_count, failure_count = meter_due_artifacts(
            self.context, now=observed_at, limit=limit
        )
        for target in targets:
            try:
                observed_size = self._occupancy_bytes(target)
                if self._record_observation(target, observed_size, observed_at) is not None:
                    metered_count += 1
            except Exception:
                failure_count += 1
                LOGGER.exception(
                    "persistent volume metering failed",
                    extra={"workspace_id": target.workspace_id, "volume_name": target.name},
                )
        return PersistentVolumeMeteringBatch(
            metered_count=metered_count,
            failure_count=failure_count,
        )

    def reconcile_volume(
        self,
        name: str,
        *,
        workspace_id: str,
        now: datetime | None = None,
    ) -> PersistentVolumeMeteringResult | None:
        observed_at = to_utc(now or utc_now())
        with self.context.database.session() as session:
            target = VolumeRepository(session).get_metering_target(
                name,
                workspace_id=workspace_id,
            )
        if target is None:
            return None
        return self._record_observation(
            target,
            self._occupancy_bytes(target),
            observed_at,
        )

    def finalize_volume_deletion(
        self,
        name: str,
        *,
        workspace_id: str,
        now: datetime | None = None,
    ) -> PersistentVolumeMeteringResult | None:
        requested_observed_at = to_utc(now) if now is not None else None
        with self.context.database.session() as session:
            target = VolumeRepository(session).get_metering_target(
                name,
                workspace_id=workspace_id,
            )
        if target is None:
            return None
        try:
            observed_size_bytes = self._occupancy_bytes(target)
        except Exception as exc:
            LOGGER.exception(
                "persistent volume final occupancy scan failed; using checkpoint estimate",
                extra={"workspace_id": target.workspace_id, "volume_name": target.name},
            )
            return self._record_observation(
                target,
                None,
                requested_observed_at or to_utc(utc_now()),
                observation_quality=MeteringObservationQuality.CheckpointEstimate,
                observation_error_type=type(exc).__name__,
            )
        return self._record_observation(
            target,
            observed_size_bytes,
            requested_observed_at or to_utc(utc_now()),
        )

    def _record_observation(
        self,
        target: VolumeMeteringTarget,
        observed_size_bytes: int | None,
        observed_at: datetime,
        *,
        observation_quality: MeteringObservationQuality = (
            MeteringObservationQuality.Authoritative
        ),
        observation_error_type: str = "",
    ) -> PersistentVolumeMeteringResult | None:
        with self.context.database.session() as session:
            WorkspaceRepository(session).lock_storage_accounting_owner(target.workspace_id)
            volumes = VolumeRepository(session)
            checkpoint = volumes.lock_metering_checkpoint(target.id)
            if checkpoint is None:
                return None
            if checkpoint.deletion_requested_at is not None:
                observed_at = min(observed_at, to_utc(checkpoint.deletion_requested_at))
            window_started_at = to_utc(checkpoint.metered_at)
            if window_started_at >= observed_at:
                return None
            elapsed_ms = (observed_at - window_started_at) // _MILLISECOND
            byte_seconds = Decimal(checkpoint.size_bytes * elapsed_ms) / _MILLISECONDS_PER_SECOND
            metadata: dict[str, JsonValue] = {
                METERING_WINDOW_STARTED_AT_METADATA_KEY: window_started_at.isoformat(),
                METERING_WINDOW_ENDED_AT_METADATA_KEY: observed_at.isoformat(),
                METERING_OBSERVATION_QUALITY_METADATA_KEY: observation_quality.value,
                "previous_size_bytes": checkpoint.size_bytes,
            }
            next_size_bytes = checkpoint.size_bytes
            if observed_size_bytes is not None:
                metadata["observed_size_bytes"] = observed_size_bytes
                next_size_bytes = observed_size_bytes
            if observation_error_type:
                metadata[METERING_OBSERVATION_ERROR_TYPE_METADATA_KEY] = observation_error_type
            record = UsageRecord(
                id=usage_record_id(
                    UsageMetric.PersistentVolumeByteSeconds.value,
                    checkpoint.workspace_id,
                    checkpoint.name,
                    window_started_at.isoformat(),
                    observed_at.isoformat(),
                ),
                workspace_id=checkpoint.workspace_id,
                resource_type="volume",
                resource_id=checkpoint.name,
                metric=UsageMetric.PersistentVolumeByteSeconds,
                # `UsageRecord.quantity` is a float, so the exact figure narrows
                # here and nowhere earlier: every step before this one is integer
                # bytes times integer milliseconds.
                quantity=float(byte_seconds),
                unit=UsageUnit.ByteSeconds,
                labels={
                    "volume_name": checkpoint.name,
                    "storage_backend": "geesefs",
                },
                metadata=metadata,
            )
            usage = UsageRepository(session)
            record = usage.append_storage(record)
            # In the transaction that wrote the record, never after it. Volume
            # storage is a billed dimension, and a window recorded without its
            # cost is money this platform measured and can no longer charge for.
            MeteredUsagePricer(session).price(record)
            volumes.advance_metering_checkpoint(
                checkpoint.id,
                size_bytes=next_size_bytes,
                metered_at=observed_at,
            )
            return PersistentVolumeMeteringResult(
                workspace_id=checkpoint.workspace_id,
                volume_name=checkpoint.name,
                previous_size_bytes=checkpoint.size_bytes,
                observed_size_bytes=observed_size_bytes,
                window_started_at=window_started_at,
                window_ended_at=observed_at,
                byte_seconds=byte_seconds,
                usage_record=record,
            )

    def _occupancy_bytes(self, target: VolumeMeteringTarget) -> int:
        return self.filesystem.occupancy_bytes(
            VolumeNamespace(workspace_id=target.workspace_id, volume_id=target.id)
        )


__all__ = [
    "PersistentVolumeMeteringBatch",
    "PersistentVolumeMeteringResult",
    "PersistentVolumeMeteringService",
]

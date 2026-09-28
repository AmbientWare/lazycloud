from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from database.tables.capacity_activations import CapacityActivationTable
from database.tables.capacity_sleep_attempts import CapacitySleepAttemptTable
from database.tables.compute import ComputeProviderInstanceTable
from shared.capacity_lifecycle import CapacityImageEvidence, CapacitySleepMode, CapacitySleepReason
from shared.contracts import ContractModel
from shared.errors import ConflictError
from shared.timestamps import to_utc, to_utc_or_none
from sqlalchemy import exists, select
from sqlalchemy.orm import Session


class CapacitySleepAttemptRecord(ContractModel):
    id: str
    instance_record_id: str
    boot_id: str
    requested_mode: CapacitySleepMode | None
    requested_at: datetime | None
    observed_at: datetime
    superseded_at: datetime | None = None
    marker_observed_at: datetime | None = None
    accepted_mode: CapacitySleepMode | None = None
    accepted_at: datetime | None = None
    provider_stopped_at: datetime | None = None
    image_evidence: CapacityImageEvidence = CapacityImageEvidence.Unknown
    evidence_at: datetime | None = None
    evidence_reason: CapacitySleepReason | None = None


def _record(row: CapacitySleepAttemptTable) -> CapacitySleepAttemptRecord:
    return CapacitySleepAttemptRecord(
        id=row.id,
        instance_record_id=row.instance_record_id,
        boot_id=row.boot_id,
        requested_mode=CapacitySleepMode(row.requested_mode) if row.requested_mode else None,
        requested_at=to_utc_or_none(row.requested_at),
        observed_at=to_utc(row.observed_at),
        superseded_at=to_utc_or_none(row.superseded_at),
        marker_observed_at=to_utc_or_none(row.marker_observed_at),
        accepted_mode=CapacitySleepMode(row.accepted_mode) if row.accepted_mode else None,
        accepted_at=to_utc_or_none(row.accepted_at),
        provider_stopped_at=to_utc_or_none(row.provider_stopped_at),
        image_evidence=CapacityImageEvidence(row.image_evidence),
        evidence_at=to_utc_or_none(row.evidence_at),
        evidence_reason=CapacitySleepReason(row.evidence_reason) if row.evidence_reason else None,
    )


@dataclass(slots=True)
class CapacitySleepAttemptRepository:
    session: Session

    def get(self, attempt_id: str) -> CapacitySleepAttemptRecord | None:
        row = self.session.get(CapacitySleepAttemptTable, attempt_id)
        return _record(row) if row is not None else None

    def get_current(self, instance_record_id: str) -> CapacitySleepAttemptRecord | None:
        row = self.session.scalar(
            select(CapacitySleepAttemptTable)
            .join(
                ComputeProviderInstanceTable,
                ComputeProviderInstanceTable.current_sleep_attempt_id
                == CapacitySleepAttemptTable.id,
            )
            .where(ComputeProviderInstanceTable.id == instance_record_id)
        )
        return _record(row) if row is not None else None

    def get_current_many(
        self, instance_record_ids: Collection[str]
    ) -> dict[str, CapacitySleepAttemptRecord]:
        if not instance_record_ids:
            return {}
        rows = self.session.scalars(
            select(CapacitySleepAttemptTable)
            .join(
                ComputeProviderInstanceTable,
                ComputeProviderInstanceTable.current_sleep_attempt_id
                == CapacitySleepAttemptTable.id,
            )
            .where(ComputeProviderInstanceTable.id.in_(instance_record_ids))
        )
        return {row.instance_record_id: _record(row) for row in rows}

    def was_activated(self, attempt_id: str) -> bool:
        return bool(
            self.session.scalar(
                select(exists().where(CapacityActivationTable.sleep_attempt_id == attempt_id))
            )
        )

    def begin(
        self,
        instance_record_id: str,
        *,
        attempt_id: str,
        boot_id: str,
        requested_mode: CapacitySleepMode | None,
        requested_at: datetime | None,
        observed_at: datetime,
    ) -> CapacitySleepAttemptRecord:
        instance = self.session.scalar(
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.id == instance_record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if instance is None:
            raise ConflictError("sleep attempt requires an existing provider instance")
        existing = self.session.get(CapacitySleepAttemptTable, attempt_id)
        if existing is not None:
            if (
                existing.instance_record_id != instance_record_id
                or instance.current_sleep_attempt_id != attempt_id
                or existing.boot_id != boot_id
                or existing.requested_mode != (requested_mode.value if requested_mode else None)
                or to_utc_or_none(existing.requested_at) != requested_at
            ):
                raise ConflictError("sleep attempt identity or intent cannot change")
            return _record(existing)
        if instance.current_sleep_attempt_id is not None:
            current = self.session.get(CapacitySleepAttemptTable, instance.current_sleep_attempt_id)
            activated = self.was_activated(instance.current_sleep_attempt_id)
            abandoned = (
                current is not None
                and current.accepted_at is None
                and current.provider_stopped_at is None
                and current.image_evidence == CapacityImageEvidence.Unavailable.value
                and current.evidence_reason == CapacitySleepReason.SaveAborted.value
            )
            superseded = current is not None and current.superseded_at is not None
            if not activated and not abandoned and not superseded:
                raise ConflictError("current sleep attempt has not been activated")
        row = CapacitySleepAttemptTable(
            id=attempt_id,
            instance_record_id=instance_record_id,
            boot_id=boot_id,
            requested_mode=requested_mode.value if requested_mode else None,
            requested_at=requested_at,
            observed_at=observed_at,
            image_evidence=CapacityImageEvidence.Unknown.value,
            evidence_reason=CapacitySleepReason.ExternalChange.value
            if requested_mode is None
            else None,
        )
        self.session.add(row)
        self.session.flush()
        instance.current_sleep_attempt_id = attempt_id
        self.session.flush()
        return _record(row)

    def observe_external_stop(
        self, instance_record_id: str, *, attempt_id: str, at: datetime
    ) -> CapacitySleepAttemptRecord:
        instance = self.session.scalar(
            select(ComputeProviderInstanceTable)
            .where(ComputeProviderInstanceTable.id == instance_record_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if instance is None:
            raise ConflictError("external stop requires an existing provider instance")
        if instance.current_sleep_attempt_id is not None:
            current = self.session.get(CapacitySleepAttemptTable, instance.current_sleep_attempt_id)
            activated = self.was_activated(instance.current_sleep_attempt_id)
            if current is not None and not activated and current.superseded_at is None:
                return _record(current)
        return self.begin(
            instance_record_id,
            attempt_id=attempt_id,
            boot_id="",
            requested_mode=None,
            requested_at=None,
            observed_at=at,
        )

    def _current(self, attempt_id: str) -> CapacitySleepAttemptTable | None:
        return self.session.scalar(
            select(CapacitySleepAttemptTable)
            .join(
                ComputeProviderInstanceTable,
                ComputeProviderInstanceTable.current_sleep_attempt_id
                == CapacitySleepAttemptTable.id,
            )
            .where(CapacitySleepAttemptTable.id == attempt_id)
            .with_for_update(of=(ComputeProviderInstanceTable, CapacitySleepAttemptTable))
            .execution_options(populate_existing=True)
        )

    def accept(
        self,
        attempt_id: str,
        *,
        mode: CapacitySleepMode,
        at: datetime,
        reason: CapacitySleepReason | None = None,
    ) -> bool:
        row = self._current(attempt_id)
        if (
            row is None
            or row.superseded_at is not None
            or (row.accepted_at is not None and at <= to_utc(row.accepted_at))
        ):
            return False
        if (
            row.accepted_mode == CapacitySleepMode.Stop.value
            and mode is CapacitySleepMode.Hibernate
        ):
            return False
        row.accepted_at = at
        row.accepted_mode = mode.value
        if mode is CapacitySleepMode.Stop:
            row.image_evidence = CapacityImageEvidence.Unavailable.value
            row.evidence_at = at
            row.evidence_reason = (reason or CapacitySleepReason.PlainStop).value
        self.session.flush()
        return True

    def abandon_unaccepted(self, attempt_id: str, *, at: datetime) -> bool:
        row = self._current(attempt_id)
        if (
            row is None
            or row.accepted_at is not None
            or row.provider_stopped_at is not None
            or at < to_utc(row.observed_at)
        ):
            return False
        row.image_evidence = CapacityImageEvidence.Unavailable.value
        row.evidence_reason = CapacitySleepReason.SaveAborted.value
        row.evidence_at = at
        self.session.flush()
        return True

    def supersede_untracked_stop(self, attempt_id: str, *, at: datetime) -> bool:
        row = self._current(attempt_id)
        if (
            row is None
            or row.provider_stopped_at is not None
            or at < to_utc(row.observed_at)
            or (row.accepted_at is not None and at < to_utc(row.accepted_at))
        ):
            return False
        if row.superseded_at is None:
            row.superseded_at = at
            self.session.flush()
        return True

    def acknowledge_marker(self, attempt_id: str, *, boot_id: str, at: datetime) -> bool:
        row = self._current(attempt_id)
        if (
            row is None
            or row.superseded_at is not None
            or row.boot_id != boot_id
            or at < to_utc(row.observed_at)
        ):
            return False
        if row.marker_observed_at is None:
            row.marker_observed_at = at
            self.session.flush()
        return True

    def record_stopped(self, attempt_id: str, *, at: datetime) -> bool:
        row = self._current(attempt_id)
        if row is None or at < to_utc(row.observed_at):
            return False
        if row.provider_stopped_at is None:
            row.provider_stopped_at = at
            self.session.flush()
        return True

    def record_evidence(
        self,
        attempt_id: str,
        *,
        evidence: CapacityImageEvidence,
        reason: CapacitySleepReason,
        at: datetime,
    ) -> bool:
        row = self._current(attempt_id)
        if (
            row is None
            or at < to_utc(row.observed_at)
            or (row.evidence_at is not None and at <= to_utc(row.evidence_at))
            or row.image_evidence != CapacityImageEvidence.Unknown.value
        ):
            return False
        if evidence is CapacityImageEvidence.Saved and (
            not row.boot_id
            or row.marker_observed_at is None
            or row.provider_stopped_at is None
            or (row.accepted_mode or row.requested_mode) != CapacitySleepMode.Hibernate.value
        ):
            raise ConflictError("saved image requires correlated hibernation and stopped evidence")
        row.image_evidence = evidence.value
        row.evidence_reason = reason.value
        row.evidence_at = at
        self.session.flush()
        return True

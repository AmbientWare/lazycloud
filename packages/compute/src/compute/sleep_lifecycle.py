from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from database.repositories.capacity_activations import CapacityActivationRepository
from database.repositories.capacity_sleep_attempts import CapacitySleepAttemptRepository
from database.types import DatabaseSession
from shared.capacity_lifecycle import (
    CapacityRestoreOutcome,
    CapacitySleepMode,
    CapacitySleepObservation,
    CapacitySleepRequest,
)
from shared.errors import ConflictError


@dataclass(frozen=True, slots=True)
class SleepObservationResult:
    acknowledgment: str = ""
    activated: bool = False


def observe_sleep(
    session: DatabaseSession,
    instance_record_id: str,
    observation: CapacitySleepObservation | None,
    *,
    boot_id: str,
    now: datetime,
) -> SleepObservationResult:
    if observation is None or observation.boot_id != boot_id:
        return SleepObservationResult()
    attempts = CapacitySleepAttemptRepository(session)
    attempt = attempts.get(observation.attempt_id)
    if attempt is None or attempt.instance_record_id != instance_record_id:
        return SleepObservationResult(observation.attempt_id)
    current = attempts.get_current(instance_record_id)
    if current is None or current.id != attempt.id or current.superseded_at is not None:
        # Retire a delayed report without applying it to a later sleep.
        return SleepObservationResult(observation.attempt_id)
    activated = observation.boot_id != attempt.boot_id or observation.suspended_seconds > 0
    if activated:
        recorded = CapacityActivationRepository(session).record_restore(
            instance_record_id,
            sleep_attempt_id=attempt.id,
            outcome=(
                CapacityRestoreOutcome.ColdBoot
                if observation.boot_id != attempt.boot_id
                else CapacityRestoreOutcome.MemoryRestored
            ),
            at=now,
        )
    else:
        recorded = attempts.acknowledge_marker(attempt.id, boot_id=boot_id, at=now)
    return SleepObservationResult(attempt.id if recorded else "", activated)


def prepare_sleep(
    session: DatabaseSession,
    instance_record_id: str,
    *,
    boot_id: str,
    mode: CapacitySleepMode,
    now: datetime,
) -> tuple[CapacitySleepRequest, bool]:
    attempts = CapacitySleepAttemptRepository(session)
    current = attempts.get_current(instance_record_id)
    consumed = current is not None and (
        current.superseded_at is not None or attempts.was_activated(current.id)
    )
    if (
        current is not None
        and not consumed
        and current.accepted_at is None
        and (
            current.boot_id != boot_id
            or current.requested_mode is not mode
            or (
                current.marker_observed_at is None
                and now - current.observed_at >= timedelta(minutes=2)
            )
        )
    ):
        if not attempts.abandon_unaccepted(current.id, at=now):
            raise ConflictError("sleep attempt still owns an unfinished stop")
        current = None
    if current is None or consumed:
        current = attempts.begin(
            instance_record_id,
            attempt_id=str(uuid4()),
            boot_id=boot_id,
            requested_mode=mode,
            requested_at=now,
            observed_at=now,
        )
    request = CapacitySleepRequest(
        attempt_id=current.id,
        boot_id=current.boot_id,
        mode=current.requested_mode or mode,
        requested_at=current.requested_at or current.observed_at,
    )
    return request, current.marker_observed_at is not None

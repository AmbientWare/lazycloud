from datetime import timedelta
from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.repositories.capacity_activations import CapacityActivationRepository
from database.repositories.capacity_sleep_attempts import CapacitySleepAttemptRepository
from database.repositories.compute import (
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.tables.capacity_activations import CapacityActivationTable
from shared.capacity_lifecycle import (
    CapacityActivationKind,
    CapacityImageEvidence,
    CapacityRestoreOutcome,
    CapacitySleepMode,
    CapacitySleepReason,
)
from shared.errors import ConflictError
from shared.timestamps import utc_now
from sqlalchemy import select


def test_sleep_evidence_is_attempt_fenced_and_held_restore_does_not_authorize(
    service_context: ServiceContext,
) -> None:
    now = utc_now()
    with service_context.database.session() as session:
        instances = ComputeProviderInstanceRepository(session)
        instance = instances.upsert(
            ComputeProviderInstanceRecord(
                id=str(uuid4()), provider="aws", offer_id="cpu", status="stopping", source="pooled"
            )
        )
        sleeps = CapacitySleepAttemptRepository(session)
        sleep = sleeps.begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id=str(uuid4()),
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=now,
            observed_at=now,
        )
        assert (
            sleeps.begin(
                instance.id,
                attempt_id=sleep.id,
                boot_id=sleep.boot_id,
                requested_mode=CapacitySleepMode.Hibernate,
                requested_at=now,
                observed_at=now,
            )
            == sleep
        )
        assert instances.upsert(instance).current_sleep_attempt_id == sleep.id
        with pytest.raises(ConflictError, match="has not been activated"):
            sleeps.begin(
                instance.id,
                attempt_id=str(uuid4()),
                boot_id=sleep.boot_id,
                requested_mode=CapacitySleepMode.Hibernate,
                requested_at=now,
                observed_at=now,
            )
        with pytest.raises(ConflictError, match="correlated"):
            sleeps.record_evidence(
                sleep.id,
                evidence=CapacityImageEvidence.Saved,
                reason=CapacitySleepReason.SaveCompleted,
                at=now,
            )
        sleeps.acknowledge_marker(sleep.id, boot_id=sleep.boot_id, at=now)
        sleeps.record_stopped(sleep.id, at=now + timedelta(seconds=10))
        assert sleeps.record_evidence(
            sleep.id,
            evidence=CapacityImageEvidence.Saved,
            reason=CapacitySleepReason.SaveCompleted,
            at=now + timedelta(seconds=11),
        )
        assert not sleeps.record_evidence(
            sleep.id,
            evidence=CapacityImageEvidence.Unknown,
            reason=CapacitySleepReason.EvidenceMissing,
            at=now + timedelta(seconds=12),
        )
        resumed_at = now + timedelta(minutes=1)
        activations = CapacityActivationRepository(session)
        activations.observe(
            instance.id,
            requested_at=resumed_at,
            kind=CapacityActivationKind.Resume,
            provider_running_at=None,
            observed_at=resumed_at,
            sleep_attempt_id=sleep.id,
        )
        assert activations.record_restore(
            instance.id,
            sleep_attempt_id=sleep.id,
            outcome=CapacityRestoreOutcome.MemoryRestored,
            at=resumed_at + timedelta(seconds=5),
        )
        activations.record_prepared(instance.id, at=resumed_at + timedelta(seconds=6))
        activation = session.scalar(
            select(CapacityActivationTable).where(
                CapacityActivationTable.instance_record_id == instance.id
            )
        )
        assert activation is not None
        assert activation.restore_outcome == CapacityRestoreOutcome.MemoryRestored.value
        assert activation.authorized_at is None and activation.ready_at is None
        next_sleep = sleeps.begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id=sleep.boot_id,
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=resumed_at + timedelta(seconds=7),
            observed_at=resumed_at + timedelta(seconds=7),
        )
        assert not sleeps.accept(
            sleep.id, mode=CapacitySleepMode.Stop, at=resumed_at + timedelta(seconds=8)
        )
        assert not activations.record_restore(
            instance.id,
            sleep_attempt_id=sleep.id,
            outcome=CapacityRestoreOutcome.ColdBoot,
            at=resumed_at + timedelta(seconds=9),
        )
        assert sleeps.get_current(instance.id) == next_sleep
        prior = sleeps.get(sleep.id)
        assert prior is not None and prior.image_evidence is CapacityImageEvidence.Saved
        assert prior.accepted_at is None


def test_untracked_stop_recovery_preserves_acceptance_and_fences_replays(
    service_context: ServiceContext,
) -> None:
    now = utc_now()
    with service_context.database.session() as session:
        instance = ComputeProviderInstanceRepository(session).upsert(
            ComputeProviderInstanceRecord(
                id=str(uuid4()), provider="aws", offer_id="cpu", status="stopping", source="pooled"
            )
        )
        sleeps = CapacitySleepAttemptRepository(session)
        old = sleeps.begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id="",
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=None,
            observed_at=now,
        )
        sleeps.accept(old.id, mode=CapacitySleepMode.Hibernate, at=now)
        recovered_at = now + timedelta(seconds=1)
        assert sleeps.supersede_untracked_stop(old.id, at=recovered_at)
        fresh_at = now + timedelta(seconds=2)
        fresh = sleeps.begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id=str(uuid4()),
            requested_mode=CapacitySleepMode.Stop,
            requested_at=fresh_at,
            observed_at=fresh_at,
        )
        assert not sleeps.supersede_untracked_stop(old.id, at=fresh_at)
        assert not sleeps.supersede_untracked_stop(fresh.id, at=recovered_at)
        previous = sleeps.get(old.id)
        assert previous is not None
        assert previous.accepted_mode is CapacitySleepMode.Hibernate
        assert previous.accepted_at == now and previous.superseded_at == recovered_at
        assert previous.requested_at is None
        assert sleeps.get_current(instance.id) == fresh


def test_external_activation_preserves_unknown_request_time_and_survives_retries(
    service_context: ServiceContext,
) -> None:
    now = utc_now()
    with service_context.database.session() as session:
        instance = ComputeProviderInstanceRepository(session).upsert(
            ComputeProviderInstanceRecord(
                id=str(uuid4()), provider="aws", offer_id="cpu", status="stopped", source="pooled"
            )
        )
        sleeps = CapacitySleepAttemptRepository(session)
        sleep = sleeps.observe_external_stop(instance.id, attempt_id=str(uuid4()), at=now)
        sleeps.record_stopped(sleep.id, at=now)
        assert (
            sleeps.observe_external_stop(instance.id, attempt_id=str(uuid4()), at=now).id
            == sleep.id
        )
        activations = CapacityActivationRepository(session)
        wake = now + timedelta(minutes=1)
        for observed in (wake, wake + timedelta(seconds=5)):
            activations.observe_external(
                instance.id,
                observed_at=observed,
                kind=CapacityActivationKind.Boot,
                provider_running_at=wake,
                sleep_attempt_id=sleep.id,
            )
        assert activations.record_restore(
            instance.id,
            sleep_attempt_id=sleep.id,
            outcome=CapacityRestoreOutcome.ColdBoot,
            at=wake + timedelta(seconds=6),
        )
        assert activations.record_restore(
            instance.id,
            sleep_attempt_id=sleep.id,
            outcome=CapacityRestoreOutcome.ColdBoot,
            at=wake + timedelta(seconds=7),
        )
        next_sleep = sleeps.begin(
            instance.id,
            attempt_id=str(uuid4()),
            boot_id=str(uuid4()),
            requested_mode=CapacitySleepMode.Hibernate,
            requested_at=wake + timedelta(seconds=8),
            observed_at=wake + timedelta(seconds=8),
        )
        activations.observe_external(
            instance.id,
            observed_at=wake,
            kind=CapacityActivationKind.Boot,
            provider_running_at=wake,
            sleep_attempt_id=next_sleep.id,
        )
        activation = session.scalars(
            select(CapacityActivationTable).where(
                CapacityActivationTable.instance_record_id == instance.id
            )
        ).one()
        assert activation.requested_at is None
        assert activation.authorized_at is None and activation.ready_at is None
        assert activation.sleep_attempt_id == sleep.id and activation.failed_at is None
        assert activation.restore_outcome == CapacityRestoreOutcome.ColdBoot.value
        assert not sleeps.was_activated(next_sleep.id)

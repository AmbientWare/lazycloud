"""Durable provider-machine records and the provider snapshots they mirror.

This owns the `ComputeProviderInstanceRecord` lifecycle: recording a launch,
mirroring a pooled provider's snapshot onto durable rows, retiring machines
whose provider instance is gone, and terminating rows whose machine failed to
become ready. `ComputeService` orchestrates workflows and calls in; nothing
here calls back out.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
    WireGuardPeerRepository,
)
from database.repositories.orchestration import (
    MachineRepository,
    WorkerRepository,
)
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from pydantic import ConfigDict, Field, JsonValue
from shared.capacity import CapacityOwnerKind
from shared.compute_enrollment import (
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
    MachineReadinessPhase,
    PrivateNetworkEnrollmentPhase,
    WireGuardPeerStatus,
)
from shared.compute_fleet import ResourceStatus
from shared.compute_policy import (
    ENDED_UNIT_PHASES,
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
)
from shared.contracts import ContractModel
from shared.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import to_utc, utc_now

from compute.agent_control import (
    MachineWorkerAvailability,
    agent_machine_worker_id,
)
from compute.context import ComputeContext
from compute.offers import (
    ComputeOffer,
    ReservationStatus,
)
from compute.providers import (
    ComputeSchedulerHooks,
    DirectMachineProvider,
    PooledCapacityProvider,
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitBootstrap,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
)
from compute.reclaim import ComputeReclaimPolicy
from compute.source_cache_storage import SourceCacheStorageLifecycleService

_LAUNCH_STATE_INTENT = "intent"


def _reservation_status_from_provider(status: str) -> ReservationStatus:
    if status == ProviderMachineStatus.Active:
        return ReservationStatus.Active
    if status == ProviderMachineStatus.Unhealthy:
        return ReservationStatus.Failed
    if status == ProviderMachineStatus.Terminated:
        return ReservationStatus.Deleted
    return ReservationStatus.Pending


def _provider_instance_metadata(
    record: ComputeProviderInstanceRecord,
) -> dict[str, JsonValue]:
    return _ProviderInstanceMetadataEnvelope.model_validate_json(record.model_dump_json()).metadata


def _provider_storage_volume_ids(record: ComputeProviderInstanceRecord) -> tuple[str, ...]:
    value = _provider_instance_metadata(record).get("storage_volume_ids")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _provider_booted_template_version(record: ComputeProviderInstanceRecord) -> str:
    value = _provider_instance_metadata(record).get("booted_template_version")
    return value if isinstance(value, str) else ""


def _metadata_time(metadata: Mapping[str, JsonValue], key: str) -> datetime | None:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _reservation_open(status: str) -> bool:
    return status not in {ReservationStatus.Deleted.value, ReservationStatus.Failed.value}


class ServiceObservation(StrEnum):
    """What one pass could tell about a machine, including nothing.

    `Unknown` is the member that matters. Folding it into `Silent` terminates a
    fleet whenever the platform itself stumbles; folding it into `Serving` clears
    the evidence against a machine that really has gone, so a machine seen as
    unknown, silent, unknown, silent would never accumulate enough to be
    reclaimed.
    """

    Serving = "serving"
    Silent = "silent"
    Unknown = "unknown"


def _require_internal_pooled_unit(
    unit: ComputeUnitRecord | None,
    *,
    unit_ref: str,
) -> ComputeUnitRecord:
    if unit is None:
        raise NotFoundError(f"compute unit not found: {unit_ref}")
    if (
        unit.visibility is not ComputeUnitVisibility.Internal
        or unit.capacity_mode is not ComputeCapacityMode.Pooled
        or unit.capacity_owner_kind is not CapacityOwnerKind.PooledProvider
    ):
        raise InvalidInputError(f"compute unit {unit_ref!r} is not provider-scaled")
    return unit


def _provider_zero_capacity_converged(snapshot: ProviderUnitSnapshot) -> bool:
    return (
        snapshot.phase is ProviderCapacityPhase.Ready
        and snapshot.desired_machines == 0
        and snapshot.observed_machines == 0
        and not snapshot.instances
    )


def _compute_pool_phase(phase: ProviderCapacityPhase) -> ComputeUnitPhase:
    return {
        ProviderCapacityPhase.Provisioning: ComputeUnitPhase.Provisioning,
        ProviderCapacityPhase.Ready: ComputeUnitPhase.Ready,
        ProviderCapacityPhase.Degraded: ComputeUnitPhase.Degraded,
        ProviderCapacityPhase.Deleting: ComputeUnitPhase.Deleting,
        ProviderCapacityPhase.Deleted: ComputeUnitPhase.Deleted,
    }[phase]


def _utc(value: datetime | None) -> datetime:
    return utc_now() if value is None else to_utc(value)


def provider_unit_request(
    pool_bootstrap_factory: ProviderUnitBootstrapFactory | None,
    pool: ComputeUnitRecord,
    offer: ComputeOffer,
) -> ProviderUnitRequest:
    if pool_bootstrap_factory is None or pool.provider_connection_id is None:
        raise RuntimeError("provider pool bootstrap is not configured")
    desired_machines, max_machines = provider_unit_operational_capacity(pool)
    return ProviderUnitRequest(
        workspace_id=pool.workspace_id,
        unit_id=pool.id,
        unit_name=pool.name,
        provider_ref=pool.provider_ref,
        provider_connection_id=pool.provider_connection_id,
        generation=pool.generation,
        offer=offer,
        desired_machines=desired_machines,
        max_machines=max_machines,
        root_volume_gib=pool.root_volume_gib,
        bootstrap=pool_bootstrap_factory.bootstrap(pool, offer),
        provider_state=pool.provider_state,
    )


def provider_unit_operational_capacity(pool: ComputeUnitRecord) -> tuple[int, int]:
    """Capacity sent to the provider, including an active replacement surge."""

    surge = max(int(bool(pool.replacement_machine_id)), int(pool.worker_rollout_surge))
    desired = pool.desired_machines + surge
    return desired, max(pool.max_machines, desired, 1)


def publish_workspace_change(
    workspace_changes: WorkspaceChangePublisher | None,
    *,
    workspace_id: str,
    topic: WorkspaceChangeTopic,
    change: WorkspaceChangeType,
    resource_id: str,
) -> None:
    if workspace_changes is None:
        return
    workspace_changes.emit_change(
        workspace_id=workspace_id,
        topic=topic,
        change=change,
        resource_id=resource_id,
    )


class _ProviderInstanceMetadataEnvelope(ContractModel):
    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ProviderUnitBootstrapFactory(Protocol):
    """What a booting node needs to reach the control plane and enrol."""

    def bootstrap(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitBootstrap: ...


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderMachineReconciler:
    """Reconciles durable provider rows against what a provider reports."""

    context: ComputeContext
    reclaim: ComputeReclaimPolicy
    pool_bootstrap_factory: ProviderUnitBootstrapFactory | None
    workspace_changes: WorkspaceChangePublisher | None
    scheduler_hooks: ComputeSchedulerHooks | None
    source_cache_lifecycle: SourceCacheStorageLifecycleService

    def _sync_pooled_instances(
        self,
        session: DatabaseSession,
        *,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
        snapshot: ProviderUnitSnapshot,
        now: datetime,
        destroyed_record_ids: set[str],
    ) -> set[str]:
        repository = ComputeProviderInstanceRepository(session)
        missing_machine_ids: set[str] = set()
        current = repository.list_for_pool(pool.id, for_update=True)
        by_instance_id = {
            item.instance_id: item for item in current if item.instance_id is not None
        }
        observed = {item.provider_instance_id: item for item in snapshot.instances}
        for instance_id, instance in observed.items():
            existing = by_instance_id.get(instance_id)
            # A provider may reuse an instance identity after a reclaimed
            # launch settled terminally. One durable row exists per pool
            # instance identity, so the closed row restarts as a fresh launch
            # attempt instead of resurrecting the reclaimed record's state.
            relaunched = existing is not None and not _reservation_open(existing.status)
            metadata: dict[str, JsonValue] = (
                _provider_instance_metadata(existing) if existing is not None else {}
            )
            metadata.pop("missing_since", None)
            if relaunched:
                for stale_key in (
                    "terminating_reason",
                    "terminated_reason",
                    "status_message",
                    "last_error",
                    "provider_storage_destroyed_at",
                ):
                    metadata.pop(stale_key, None)
            settled_existing = existing if existing is not None and not relaunched else None
            provider_status = _reservation_status_from_provider(instance.status).value
            if (
                settled_existing is not None
                and settled_existing.status == ReservationStatus.Terminating.value
            ):
                provider_status = ReservationStatus.Terminating.value
            bootstrap_phase = (
                settled_existing.bootstrap_phase
                if settled_existing is not None
                else MachineBootstrapPhase.Provisioning
            )
            bootstrap_failure_reason = (
                settled_existing.bootstrap_failure_reason if settled_existing is not None else None
            )
            if bootstrap_phase is MachineBootstrapPhase.Requested:
                bootstrap_phase = MachineBootstrapPhase.Provisioning
            if instance.status == ProviderMachineStatus.Unhealthy:
                bootstrap_phase = MachineBootstrapPhase.Failed
                bootstrap_failure_reason = MachineBootstrapFailureReason.ProviderStopped
            launch_attempt = (
                settled_existing.launch_attempt
                if settled_existing is not None
                else max((item.launch_attempt for item in current), default=0) + 1
            )
            bootstrap_observed_at = (
                settled_existing.bootstrap_observed_at
                if settled_existing is not None
                and bootstrap_phase is settled_existing.bootstrap_phase
                else now
            )
            payload: dict[str, JsonValue | datetime] = {
                "provider": pool.provider_ref,
                "offer_id": offer.id,
                "status": provider_status,
                "source": "workspace_policy",
                "pool_id": pool.id,
                "instance_type": offer.instance_type,
                "instance_id": instance_id,
                "machine_id": settled_existing.machine_id if settled_existing is not None else None,
                "gpu": offer.gpu,
                "gpu_count": offer.gpu_count,
                "cpu_millicores": offer.cpu_millicores,
                "memory_mb": offer.memory_mb,
                "hourly_cost_micros": offer.hourly_cost_micros,
                "committed_micros": 0,
                "expires_at": None,
                "billing_renewal_at": None,
                "bootstrap_phase": bootstrap_phase,
                "bootstrap_failure_reason": bootstrap_failure_reason,
                "bootstrap_observed_at": bootstrap_observed_at,
                # Named rather than omitted, because an omitted key on the
                # relaunch path keeps the reclaimed launch's value: a new
                # machine would inherit a proof that some earlier machine once
                # served, and never be judged as the new machine it is.
                "first_enrolled_at": (
                    settled_existing.first_enrolled_at if settled_existing is not None else None
                ),
                "first_served_at": (
                    settled_existing.first_served_at if settled_existing is not None else None
                ),
                "last_served_at": (
                    settled_existing.last_served_at if settled_existing is not None else None
                ),
                "unserved_observations": (
                    settled_existing.unserved_observations if settled_existing is not None else 0
                ),
                "launch_attempt": launch_attempt,
                "metadata": {
                    **metadata,
                    "architecture": offer.architecture,
                    "runtime": offer.runtime,
                    "region": offer.region,
                    "storage_mb": offer.storage_mb,
                    "availability_zone": instance.availability_zone,
                    "storage_volume_ids": list(instance.storage_volume_ids),
                    "booted_template_version": instance.booted_template_version,
                },
            }
            if existing is None:
                repository.records.create(payload, status=provider_status)
            else:
                repository.upsert(existing.model_copy(update=payload))
        for existing in current:
            if existing.instance_id is not None and existing.instance_id in observed:
                continue
            existing_metadata = _provider_instance_metadata(existing)
            missing_since = _metadata_time(existing_metadata, "missing_since")
            if missing_since is None:
                existing_metadata["missing_since"] = now.isoformat()
                missing_since = now
            elif not _reservation_open(existing.status):
                # A settled row with its absence already recorded has nothing
                # left to say. Writing it anyway moves `updated_at`, and the
                # pool's scale-down cooldown reads the newest write across
                # closed rows as the moment capacity was last released: rows
                # closed two days ago were re-dated every pass, so the cooldown
                # was re-armed exactly as fast as it expired and the idle drain
                # was never reached.
                continue
            if (
                (now - missing_since).total_seconds() < 120
                and snapshot.phase is not ProviderCapacityPhase.Deleted
                and not _provider_zero_capacity_converged(snapshot)
            ):
                repository.upsert(
                    existing.model_copy(update={"metadata": existing_metadata, "updated_at": now})
                )
                continue
            if existing.id not in destroyed_record_ids:
                repository.upsert(
                    existing.model_copy(update={"metadata": existing_metadata, "updated_at": now})
                )
                continue
            if (
                existing.machine_id
                and not self.source_cache_lifecycle.record_machine_storage_destroyed_in_session(
                    session,
                    existing.machine_id,
                    observed_at=now,
                )
            ):
                repository.upsert(
                    existing.model_copy(update={"metadata": existing_metadata, "updated_at": now})
                )
                continue
            repository.upsert(
                existing.model_copy(
                    update={
                        "status": ReservationStatus.Deleted.value,
                        "metadata": {
                            **existing_metadata,
                            "terminated_reason": "provider_instance_missing",
                            "provider_storage_destroyed_at": now.isoformat(),
                        },
                        "updated_at": now,
                    }
                )
            )
            if existing.machine_id:
                missing_machine_ids.add(existing.machine_id)
        return missing_machine_ids

    def _apply_pooled_snapshot(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
        snapshot: ProviderUnitSnapshot,
        *,
        provider: PooledCapacityProvider,
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        current_time = _utc(now)
        phase = _compute_pool_phase(snapshot.phase)
        if phase is ComputeUnitPhase.Deleted and pool.phase not in ENDED_UNIT_PHASES:
            # An account with no autoscaling group in it is what a torn-down unit
            # and an unbuilt one both look like, and the provider is asked about
            # the account. Only the unit knows which it is, so a unit nobody asked
            # to stop is one the provider has not built yet.
            #
            # Getting this wrong is not symmetric. `reconcile_pooled_capacity`
            # declines to build a deleted unit, so a wrong `Deleted` is the end of
            # the unit: the warm baseline revives it, the next snapshot buries it
            # again, and the pool holds a floor it can never fill. A wrong
            # `Provisioning` costs one pass.
            phase = ComputeUnitPhase.Provisioning
        # Providers never own the relaunch bookkeeping: carry it from the durable
        # intent so snapshot application cannot silently restore a pool that
        # exhausted its launch attempts, nor discard the baseline that recovery
        # set. Explicit capacity mutations clear the reason on the intent before
        # the provider is consulted. A provider describes machines it holds; it
        # has never heard of either field, so its snapshot leaves both at their
        # defaults and overwriting from it loses them.
        provider_state = snapshot.provider_state.model_copy(
            update={
                "degraded_reason": pool.provider_state.degraded_reason,
                "degraded_at": pool.provider_state.degraded_at,
                "launch_attempt_baseline": pool.provider_state.launch_attempt_baseline,
            }
        )
        provider_request = provider_unit_request(self.pool_bootstrap_factory, pool, offer)
        observed_instance_ids = {item.provider_instance_id for item in snapshot.instances}
        authoritative_zero = _provider_zero_capacity_converged(snapshot)
        with self.context.database.session() as session:
            prior_instances = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        destroyed_record_ids: set[str] = set()
        for instance in prior_instances:
            if instance.instance_id is not None and instance.instance_id in observed_instance_ids:
                continue
            missing_since = _metadata_time(
                _provider_instance_metadata(instance),
                "missing_since",
            )
            settled = (
                authoritative_zero
                or snapshot.phase is ProviderCapacityPhase.Deleted
                or (
                    missing_since is not None
                    and (current_time - missing_since).total_seconds() >= 120
                )
            )
            if not settled:
                continue
            storage_volume_ids = _provider_storage_volume_ids(instance)
            if instance.instance_id is None:
                if authoritative_zero and not storage_volume_ids:
                    destroyed_record_ids.add(instance.id)
                continue
            if provider.machine_storage_destroyed(
                provider_request,
                instance.instance_id,
                storage_volume_ids,
            ):
                destroyed_record_ids.add(instance.id)
        unproved = {
            item.id
            for item in prior_instances
            if _reservation_open(item.status) and item.id not in destroyed_record_ids
        }
        if unproved:
            if snapshot.phase is ProviderCapacityPhase.Deleted:
                phase = ComputeUnitPhase.Deleting
            elif snapshot.desired_machines != snapshot.observed_machines or authoritative_zero:
                phase = ComputeUnitPhase.Updating
        if provider_state.degraded_reason is not None and phase not in {
            ComputeUnitPhase.Deleting,
            ComputeUnitPhase.Deleted,
        }:
            phase = ComputeUnitPhase.Degraded
        missing_machine_ids: set[str]
        with self.context.database.session() as session:
            repository = ComputeUnitRepository(session)
            updated = repository.apply_provider_state(
                pool.id,
                generation=pool.generation,
                observed_machines=snapshot.observed_machines,
                phase=phase,
                provider_state=provider_state,
            )
            if updated is None:
                current = repository.get(pool.id)
                if current is None:
                    raise RuntimeError(f"compute pool disappeared during reconciliation: {pool.id}")
                return current
            missing_machine_ids = self._sync_pooled_instances(
                session,
                pool=updated,
                offer=offer,
                snapshot=snapshot,
                now=current_time,
                destroyed_record_ids=destroyed_record_ids,
            )
        for machine_id in missing_machine_ids:
            self.retire_provider_pool_machine(
                updated.workspace_id,
                updated.capacity_owner_id,
                machine_id,
                reason="provider instance storage destroyed",
                now=current_time,
            )
        if self.scheduler_hooks is not None:
            self.scheduler_hooks.register_internal_unit(updated, offer)
        publish_workspace_change(
            self.workspace_changes,
            workspace_id=updated.workspace_id,
            topic=WorkspaceChangeTopic.ComputeUnits,
            change=WorkspaceChangeType.Updated,
            resource_id=updated.id,
        )
        return updated

    def _terminate_provider_record(
        self,
        session: DatabaseSession,
        record: ComputeProviderInstanceRecord,
        *,
        clients: Mapping[str, DirectMachineProvider],
        reason: str,
        message: str,
        bootstrap_failure_reason: MachineBootstrapFailureReason | None = None,
        bootstrap_observed_at: datetime | None = None,
        deleting_workspace_id: str | None = None,
    ) -> bool:
        if not _reservation_open(record.status):
            return False
        if bootstrap_failure_reason is not None:
            record = record.model_copy(
                update={
                    "bootstrap_phase": MachineBootstrapPhase.Failed,
                    "bootstrap_failure_reason": bootstrap_failure_reason,
                    "bootstrap_failure_detail": message,
                    "bootstrap_observed_at": _utc(bootstrap_observed_at),
                    "updated_at": _utc(bootstrap_observed_at),
                }
            )
            ComputeProviderInstanceRepository(session).upsert(record)
        client = clients.get(record.provider)
        status = ReservationStatus.Terminating.value
        last_error = ""
        provider_storage_destroyed_at: datetime | None = None
        if client is not None:
            try:
                provider_instance_id = record.instance_id or record.id
                client.terminate_machine(provider_instance_id)
                if client.machine_storage_destroyed(
                    provider_instance_id,
                    _provider_storage_volume_ids(record),
                ):
                    provider_storage_destroyed_at = utc_now()
                    cache_retired = (
                        not record.machine_id
                        or self.source_cache_lifecycle.record_machine_storage_destroyed_in_session(
                            session,
                            record.machine_id,
                            observed_at=provider_storage_destroyed_at,
                        )
                    )
                    if cache_retired:
                        status = ReservationStatus.Deleted.value
            except Exception as exc:
                last_error = str(exc)
        metadata: dict[str, JsonValue] = {
            **_provider_instance_metadata(record),
            "terminating_reason": reason,
            "status_message": message,
            "last_error": last_error,
        }
        if provider_storage_destroyed_at is not None:
            metadata["provider_storage_destroyed_at"] = provider_storage_destroyed_at.isoformat()
        updated = record.model_copy(
            update={
                "status": status,
                "metadata": metadata,
            }
        )
        ComputeProviderInstanceRepository(session).upsert(updated)
        self._revoke_provider_join_credential(
            session,
            record,
            deleting_workspace_id=deleting_workspace_id,
        )
        if record.machine_id and self.scheduler_hooks is not None:
            self.scheduler_hooks.disable_machine(record.machine_id, reason)
        if record.machine_id:
            machine = MachineRepository(session).get_across_workspaces(record.machine_id)
            if machine is not None and status == ReservationStatus.Deleted.value:
                if deleting_workspace_id is not None:
                    MachineRepository(session).mark_deleted_for_workspace_deletion(
                        machine.id,
                        workspace_id=deleting_workspace_id,
                    )
                else:
                    MachineRepository(session).upsert(
                        machine.model_copy(update={"status": ResourceStatus.Deleted})
                    )
        return status == ReservationStatus.Deleted.value

    def _retire_provider_pool_machines(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        *,
        machine_ids: set[str] | None,
        reason: str,
        now: datetime,
    ) -> tuple[str, ...]:
        hot_state_retirements: list[ComputeMachineEnrollmentRecord] = []
        join_token_hashes: set[str] = set()
        changed_machine_ids: list[str] = []
        with self.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            machines = MachineRepository(session)
            workers = WorkerRepository(session)
            credentials = ComputeJoinCredentialRepository(session)
            for enrollment in enrollments.list_for_unit(workspace_id, capacity_owner_id):
                if machine_ids is not None and enrollment.machine_id not in machine_ids:
                    continue
                hot_state_retirements.append(enrollment)
                if enrollment.status is not ComputeMachineEnrollmentStatus.Deleted:
                    self._schedule_provider_machine_identity_cleanup(session, enrollment, now=now)
                    enrollments.save(
                        enrollment.model_copy(
                            update={
                                "status": ComputeMachineEnrollmentStatus.Deleted,
                                "heartbeat_confirmed": False,
                                "schedulable": False,
                                "readiness_phase": MachineReadinessPhase.Revoked,
                                "network_phase": PrivateNetworkEnrollmentPhase.Revoked,
                                "last_disconnect_at": now,
                                "revoked_at": enrollment.revoked_at or now,
                                "updated_at": now,
                            }
                        )
                    )
                    changed_machine_ids.append(enrollment.machine_id)
                machine = machines.get(enrollment.machine_id, workspace_id=workspace_id)
                if machine is not None and machine.status is not ResourceStatus.Deleted:
                    machines.upsert(
                        machine.model_copy(
                            update={"status": ResourceStatus.Deleted, "updated_at": now}
                        ),
                        workspace_id=workspace_id,
                    )
                worker = workers.get(
                    agent_machine_worker_id(enrollment.machine_id),
                    workspace_id=workspace_id,
                )
                if worker is not None and worker.status is not ResourceStatus.Deleted:
                    workers.upsert(
                        worker.model_copy(update={"status": ResourceStatus.Deleted}),
                        workspace_id=workspace_id,
                    )
            for credential in credentials.list_for_unit(
                workspace_id,
                capacity_owner_id,
                for_update=True,
            ):
                join_token_hashes.add(credential.token_hash)
                if credential.status is ComputeCredentialStatus.Active:
                    credentials.save(credential.revoke(now=now))

        if self.scheduler_hooks is not None:
            for enrollment in hot_state_retirements:
                self.scheduler_hooks.retire_machine(
                    enrollment.workspace_id,
                    enrollment.machine_id,
                    reason,
                )
            for token_hash in join_token_hashes:
                self.scheduler_hooks.revoke_unit_join_token(token_hash)
        for machine_id in changed_machine_ids:
            publish_workspace_change(
                self.workspace_changes,
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeMachines,
                change=WorkspaceChangeType.Deleted,
                resource_id=machine_id,
            )
        return tuple(changed_machine_ids)

    def retire_provider_pool_machine(
        self,
        workspace_id: str,
        capacity_owner_id: str,
        machine_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        return self._retire_provider_pool_machines(
            workspace_id,
            capacity_owner_id,
            machine_ids={machine_id},
            reason=reason,
            now=_utc(now),
        )

    def _observe_provider_service_state(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        record: ComputeProviderInstanceRecord,
        *,
        pool_reachable: bool,
    ) -> ServiceObservation:
        """Whether this machine is serving, not serving, or cannot be judged.

        Runs for every open record on every pass, ahead of any deadline. The
        reclaim used to look only at machines it had already decided were late,
        which left it with no memory of the ones that were working: a machine
        healthy for a week and silent for a minute presented exactly like one
        that had never started.
        """

        if not pool_reachable:
            return ServiceObservation.Unknown
        if record.machine_id is None:
            return ServiceObservation.Silent
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            pool.workspace_id,
            record.machine_id,
        )
        if enrollment is None:
            return ServiceObservation.Silent
        if self.scheduler_hooks is None:
            msg = "provider bootstrap reclaim requires scheduler worker state"
            raise RuntimeError(msg)
        try:
            availability = self.scheduler_hooks.machine_worker_availability(record.machine_id)
        except Exception:
            # This answer decides whether a billable machine is terminated. An
            # unreachable worker-state store is "unknown", and unknown is not an
            # observation: it neither counts against the machine nor clears what
            # counted against it before.
            LOGGER.exception(
                "worker state was unreachable while observing machine %s; keeping it",
                record.machine_id,
            )
            return ServiceObservation.Unknown
        if availability is MachineWorkerAvailability.Unknown:
            # Asked before the enrollment, because a missing worker record leaves
            # nothing to disagree with. The record is a cache the worker re-arms
            # as it reports, so its absence is a gap in what we heard rather than
            # an answer about the machine.
            return ServiceObservation.Unknown
        if (
            availability is MachineWorkerAvailability.Available
            and enrollment.status is ComputeMachineEnrollmentStatus.Active
            and enrollment.readiness_phase is MachineReadinessPhase.Ready
        ):
            return ServiceObservation.Serving
        return ServiceObservation.Silent

    def _record_service_observation(
        self,
        session: DatabaseSession,
        record: ComputeProviderInstanceRecord,
        observation: ServiceObservation,
        *,
        now: datetime,
    ) -> ComputeProviderInstanceRecord:
        """Persist what this pass saw, so the next pass is not the first one."""

        if observation is ServiceObservation.Unknown:
            return record
        if observation is ServiceObservation.Serving:
            if (
                record.first_served_at is not None
                and record.last_served_at == now
                and record.unserved_observations == 0
            ):
                return record
            update: dict[str, object] = {
                "last_served_at": now,
                "unserved_observations": 0,
                "updated_at": now,
            }
            if record.first_served_at is None:
                update["first_served_at"] = now
            return ComputeProviderInstanceRepository(session).upsert(
                record.model_copy(update=update)
            )
        return ComputeProviderInstanceRepository(session).upsert(
            record.model_copy(
                update={
                    "unserved_observations": record.unserved_observations + 1,
                    "updated_at": now,
                }
            )
        )

    def _provider_bootstrap_failure_to_reclaim(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        record: ComputeProviderInstanceRecord,
        *,
        now: datetime,
        observing_since: datetime,
        live_containers: int,
    ) -> MachineBootstrapFailureReason | None:
        """Whether to take this machine away, over an already-recorded history.

        Two regimes, split by whether the platform ever saw the machine serve.
        Before that, a machine is judged on how long it has been stuck in one
        bootstrap phase. After it, the bootstrap deadlines no longer say anything
        true — `joining` is the last phase the model has, so a working machine
        sits past its deadline for as long as it lives — and what matters instead
        is how long it has been since it last worked.
        """

        if record.bootstrap_phase is MachineBootstrapPhase.Failed:
            # The node named its own failure. Reclaim it under that reason rather
            # than re-diagnosing it as a timeout it did not have.
            deadline = self.reclaim.phase_deadline_for(record.provider, record.bootstrap_phase)
            if deadline is None or not self._deadline_elapsed(
                record, now, observing_since, deadline
            ):
                return None
            return record.bootstrap_failure_reason or MachineBootstrapFailureReason.Unknown
        if record.first_served_at is not None:
            return self._service_loss_to_reclaim(
                record,
                now=now,
                observing_since=observing_since,
                live_containers=live_containers,
            )
        deadline = self.reclaim.phase_deadline_for(record.provider, record.bootstrap_phase)
        if deadline is None:
            return None
        if not self._deadline_elapsed(record, now, observing_since, deadline):
            return None
        if record.unserved_observations < self.reclaim.bootstrap_failure_observations:
            return None
        if record.bootstrap_phase in {
            MachineBootstrapPhase.Requested,
            MachineBootstrapPhase.Provisioning,
            MachineBootstrapPhase.Booting,
        }:
            return MachineBootstrapFailureReason.BootstrapTimedOut
        if record.machine_id is None:
            # A record that reached `joining` proved a machine was bound to it,
            # and the column is cleared by the foreign key when that machine row
            # is deleted. Reporting a bootstrap timeout for one of those blames
            # the boot for a deletion that happened long after it.
            if record.first_enrolled_at is not None:
                return MachineBootstrapFailureReason.MachineRecordDeleted
            return MachineBootstrapFailureReason.BootstrapTimedOut
        return MachineBootstrapFailureReason.WorkerReadinessFailed

    def _service_loss_to_reclaim(
        self,
        record: ComputeProviderInstanceRecord,
        *,
        now: datetime,
        observing_since: datetime,
        live_containers: int,
    ) -> MachineBootstrapFailureReason | None:
        if record.unserved_observations < self.reclaim.service_loss_observations:
            return None
        last_served_at = _utc(record.last_served_at or record.first_served_at or record.created_at)
        silent_since = max(last_served_at, observing_since)
        if now - silent_since < self.reclaim.service_loss_window:
            return None
        if live_containers > 0 and now - silent_since < self.reclaim.live_container_reclaim_grace:
            # Work still claims this machine. The claim is bounded rather than
            # absolute: container rows outlive the worker that owned them when
            # nothing ticks to settle them, and a veto with no end is a meter
            # with no end.
            return None
        return MachineBootstrapFailureReason.ServiceLost

    def _deadline_elapsed(
        self,
        record: ComputeProviderInstanceRecord,
        now: datetime,
        observing_since: datetime,
        deadline: timedelta,
    ) -> bool:
        """Deadlines run from when someone was listening, not from wall clock.

        A control plane that was down was not observing, and every machine it
        serves looks silent for exactly as long as it was away. Starting the
        clock at the later of the two means a restart costs a machine its
        deadline afresh rather than costing the fleet its life.
        """

        phase_started_at = _utc(record.bootstrap_observed_at or record.created_at)
        return now - max(phase_started_at, observing_since) >= deadline

    def _persist_zero_capacity_repair(
        self,
        unit: ComputeUnitRecord,
        *,
        maximum: int,
        observed: ProviderUnitSnapshot,
    ) -> ComputeUnitRecord:
        with self.context.database.session() as session:
            pools = ComputeUnitRepository(session)
            current = _require_internal_pooled_unit(
                pools.get(unit.id, for_update=True),
                unit_ref=unit.id,
            )
            if (
                current.capacity_owner_id != unit.capacity_owner_id
                or current.generation != unit.generation
                or current.desired_machines != 0
            ):
                raise ConflictError(
                    f"compute pool {unit.name!r} zero-capacity repair was superseded"
                )
            intent = pools.update_capacity(
                current.id,
                expected_generation=current.generation,
                desired_machines=0,
                max_machines=maximum,
                observed_machines=observed.observed_machines,
                phase=ComputeUnitPhase.Updating,
                provider_state=observed.provider_state,
            )
            if intent is None:
                raise ConflictError(
                    f"compute pool {unit.name!r} zero-capacity repair was superseded"
                )
            return intent

    @staticmethod
    def _schedule_provider_machine_identity_cleanup(
        session: DatabaseSession,
        enrollment: ComputeMachineEnrollmentRecord,
        *,
        now: datetime,
    ) -> None:
        peers = WireGuardPeerRepository(session)
        peer = peers.by_enrollment(enrollment.id, for_update=True)
        if peer is None or peer.status is WireGuardPeerStatus.Revoked:
            return
        peers.save(
            peer.model_copy(
                update={
                    "status": WireGuardPeerStatus.Revoked,
                    "revoked_at": now,
                    "updated_at": now,
                }
            )
        )

    def _revoke_provider_join_credential(
        self,
        session: DatabaseSession,
        record: ComputeProviderInstanceRecord,
        *,
        deleting_workspace_id: str | None = None,
    ) -> None:
        token_hash = str(_provider_instance_metadata(record).get("registration_token_hash") or "")
        if token_hash == "":
            return
        credentials = ComputeJoinCredentialRepository(session)
        credential = credentials.get_by_hash(token_hash, for_update=True)
        if credential is not None and credential.status is ComputeCredentialStatus.Active:
            revoked = credential.revoke(now=utc_now())
            if deleting_workspace_id is not None:
                credentials.save_for_workspace_deletion(revoked)
            else:
                credentials.save(revoked)

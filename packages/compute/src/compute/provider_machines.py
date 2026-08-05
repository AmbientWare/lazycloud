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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from database.repositories.compute import (
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
    TailnetCleanupTombstoneRepository,
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
    TailnetEnrollmentPhase,
)
from shared.compute_fleet import Machine, ResourceStatus
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
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
    agent_machine_worker_id,
    machine_serves_workloads,
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

PROVIDER_MACHINE_IDENTITY_SETTLE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class LaunchedProviderInstance:
    """A provider instance created during one launch flow, keyed to its owner."""

    provider: str
    provider_instance_id: str
    machine_id: str
    status: str
    address: str
    storage_volume_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedProviderLaunch:
    """Durable provider launch intent plus its one-time registration credential."""

    provider: str
    offer: ComputeOffer
    machine: Machine
    provider_record_id: str
    registration_token: str = field(repr=False)


_LAUNCH_STATE_INTENT = "intent"


_LAUNCH_STATE_COMMITTED = "committed"


_LAUNCH_STATE_COMPENSATING = "compensating"


_LAUNCH_STATE_COMPENSATED = "compensated"


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


def _provider_launch_state(record: ComputeProviderInstanceRecord) -> str:
    return str(_provider_instance_metadata(record).get("launch_state") or "")


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


def _require_internal_pooled_unit(
    unit: ComputeUnitRecord | None,
    *,
    unit_name: UnitName,
) -> ComputeUnitRecord:
    if unit is None:
        raise NotFoundError(f"compute unit not found: {unit_name}")
    if (
        unit.visibility is not ComputeUnitVisibility.Internal
        or unit.capacity_mode is not ComputeCapacityMode.Pooled
        or unit.capacity_owner_kind is not CapacityOwnerKind.PooledProvider
    ):
        raise InvalidInputError(f"compute unit {unit_name!r} is not provider-scaled")
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


def _unique_nonempty(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _whole_hours(seconds: int) -> int:
    if seconds <= 0:
        return 0
    return max((seconds + 3599) // 3600, 1)


def _utc(value: datetime | None) -> datetime:
    return utc_now() if value is None else to_utc(value)


def provider_unit_request(
    pool_bootstrap_factory: ProviderUnitBootstrapFactory | None,
    pool: ComputeUnitRecord,
    offer: ComputeOffer,
) -> ProviderUnitRequest:
    if pool_bootstrap_factory is None or pool.provider_connection_id is None:
        raise RuntimeError("provider pool bootstrap is not configured")
    return ProviderUnitRequest(
        workspace_id=pool.workspace_id,
        unit_id=pool.id,
        unit_name=pool.name,
        provider_ref=pool.provider_ref,
        provider_connection_id=pool.provider_connection_id,
        generation=pool.generation,
        offer=offer,
        desired_machines=pool.desired_machines,
        max_machines=pool.max_machines,
        root_volume_gib=pool.root_volume_gib,
        bootstrap=pool_bootstrap_factory.bootstrap(pool, offer),
        provider_state=pool.provider_state,
    )


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
    stale_first_seen: dict[tuple[str, str, str], datetime] = field(default_factory=dict)

    def _reconcile_provider_machines(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        instances: list[ComputeProviderInstanceRecord],
        *,
        clients: Mapping[str, DirectMachineProvider],
        now: datetime,
    ) -> tuple[bool, set[str], set[str]]:
        changed = False
        updated_machine_ids: set[str] = set()
        reclaimed_machine_ids: set[str] = set()
        by_provider: dict[str, list[ComputeProviderInstanceRecord]] = {}
        for record in instances:
            if _reservation_open(record.status):
                by_provider.setdefault(record.provider, []).append(record)
        for provider_name, records in by_provider.items():
            client = clients.get(provider_name)
            if client is None:
                continue
            overdue_intents = [
                record
                for record in records
                if _provider_launch_state(record) == _LAUNCH_STATE_INTENT
                and self._launch_intent_overdue(record, now=now)
            ]
            overdue_machine_ids = {
                record.machine_id for record in overdue_intents if record.machine_id
            }
            expected = {
                record.machine_id
                for record in records
                if record.machine_id and record.machine_id not in overdue_machine_ids
            }
            probe = client.reconcile_machines(pool.pool, expected, terminate_stale=False)
            stale = set(probe.stale_machine_ids)
            if overdue_machine_ids:
                observed = {item.machine_id: item for item in probe.observed_machines}
                provider_instances = ComputeProviderInstanceRepository(session)
                machines = MachineRepository(session)
                for record in overdue_intents:
                    if record.machine_id is None:
                        continue
                    remote = observed.get(record.machine_id)
                    updated = record.model_copy(
                        update={
                            "instance_id": (
                                remote.provider_instance_id
                                if remote is not None
                                else record.instance_id
                            ),
                            "status": (
                                ReservationStatus.Terminating.value
                                if remote is not None
                                else ReservationStatus.Failed.value
                            ),
                            "metadata": {
                                **_provider_instance_metadata(record),
                                "storage_volume_ids": (
                                    list(remote.storage_volume_ids) if remote is not None else []
                                ),
                                "launch_state": (
                                    _LAUNCH_STATE_COMPENSATING
                                    if remote is not None
                                    else _LAUNCH_STATE_COMPENSATED
                                ),
                            },
                            "updated_at": utc_now(),
                        }
                    )
                    provider_instances.upsert(updated)
                    reclaimed = False
                    if remote is not None:
                        reclaimed = self._terminate_provider_record(
                            session,
                            updated,
                            clients=clients,
                            reason="abandoned_launch_intent",
                            message="provider launch did not commit to durable active state",
                        )
                    self._revoke_provider_join_credential(session, record)
                    machine = machines.get_across_workspaces(record.machine_id)
                    if machine is not None:
                        if reclaimed:
                            machines.upsert(
                                machine.model_copy(update={"status": ResourceStatus.Deleted})
                            )
                        elif remote is None:
                            machines.upsert(
                                machine.model_copy(update={"status": ResourceStatus.Failed})
                            )
                    if reclaimed:
                        reclaimed_machine_ids.add(record.machine_id)
                    else:
                        updated_machine_ids.add(record.machine_id)
                    changed = True
                records = [
                    record for record in records if record.machine_id not in overdue_machine_ids
                ]
            self._terminate_overdue_stale_machines(
                client,
                provider_name=provider_name,
                pool=pool.pool,
                expected=expected,
                stale=stale - overdue_machine_ids,
                now=now,
            )
            missing = set(probe.missing_machine_ids)
            for record in records:
                if record.machine_id is not None and record.machine_id in missing:
                    if _provider_launch_state(record) == _LAUNCH_STATE_INTENT:
                        continue
                    reclaimed = self._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="provider_machine_missing",
                        message=(
                            "provider active inventory is missing the machine; "
                            "requesting termination and exact absence proof"
                        ),
                    )
                    if reclaimed:
                        reclaimed_machine_ids.add(record.machine_id)
                    else:
                        updated_machine_ids.add(record.machine_id)
                    changed = True
                    continue
                bootstrap_failure = self._provider_bootstrap_failure_to_reclaim(
                    session,
                    pool,
                    record,
                    now=now,
                )
                if bootstrap_failure is not None:
                    reclaimed = self._terminate_provider_record(
                        session,
                        record,
                        clients=clients,
                        reason="bootstrap_deadline_exceeded",
                        message=(
                            "machine did not produce an available worker before "
                            "the bootstrap phase deadline"
                        ),
                        bootstrap_failure_reason=bootstrap_failure,
                        bootstrap_observed_at=now,
                    )
                    if not reclaimed:
                        continue
                    changed = True
                    if record.machine_id is not None:
                        reclaimed_machine_ids.add(record.machine_id)
                    LOGGER.warning(
                        "reclaimed provider machine that did not become ready",
                        extra={
                            "provider": provider_name,
                            "pool": pool.name,
                            "machine_id": record.machine_id,
                            "provider_instance_id": record.instance_id or record.id,
                        },
                    )
        return changed, updated_machine_ids, reclaimed_machine_ids

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
        update_capacity: bool,
        now: datetime | None = None,
    ) -> ComputeUnitRecord:
        current_time = _utc(now)
        phase = _compute_pool_phase(snapshot.phase)
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
            if update_capacity:
                updated = repository.update_capacity(
                    pool.id,
                    expected_generation=pool.generation,
                    desired_machines=snapshot.desired_machines,
                    max_machines=max(snapshot.max_machines, snapshot.desired_machines, 1),
                    observed_machines=snapshot.observed_machines,
                    phase=phase,
                    provider_state=provider_state,
                )
            else:
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

    def _record_failed_launch_cleanup(
        self,
        prepared_launches: list[PreparedProviderLaunch],
        *,
        created_instances: list[LaunchedProviderInstance],
        attempted_machine_ids: set[str],
        terminated_machine_ids: set[str],
        failure: str,
    ) -> None:
        created_by_machine = {item.machine_id: item for item in created_instances}
        with self.context.database.session() as session:
            provider_instances = ComputeProviderInstanceRepository(session)
            machines = MachineRepository(session)
            for prepared in prepared_launches:
                record = provider_instances.records.get(prepared.provider_record_id)
                if record is None:
                    continue
                created = created_by_machine.get(prepared.machine.id)
                outcome_unknown = created is None and prepared.machine.id in attempted_machine_ids
                terminated = prepared.machine.id in terminated_machine_ids
                if outcome_unknown:
                    provider_instances.upsert(
                        record.model_copy(
                            update={
                                "bootstrap_phase": MachineBootstrapPhase.Failed,
                                "bootstrap_failure_reason": (MachineBootstrapFailureReason.Unknown),
                                "bootstrap_observed_at": utc_now(),
                                "metadata": {
                                    **_provider_instance_metadata(record),
                                    "last_error": failure,
                                    "launch_outcome": "unknown",
                                },
                                "updated_at": utc_now(),
                            }
                        )
                    )
                    continue
                status = (
                    ReservationStatus.Deleted.value
                    if terminated
                    else (
                        ReservationStatus.Terminating.value
                        if created is not None
                        else ReservationStatus.Failed.value
                    )
                )
                provider_instances.upsert(
                    record.model_copy(
                        update={
                            "instance_id": (
                                created.provider_instance_id
                                if created is not None
                                else record.instance_id
                            ),
                            "status": status,
                            "bootstrap_phase": MachineBootstrapPhase.Failed,
                            "bootstrap_failure_reason": MachineBootstrapFailureReason.Unknown,
                            "bootstrap_observed_at": utc_now(),
                            "metadata": {
                                **_provider_instance_metadata(record),
                                "storage_volume_ids": (
                                    list(created.storage_volume_ids) if created is not None else []
                                ),
                                "launch_state": (
                                    _LAUNCH_STATE_COMPENSATED
                                    if terminated or created is None
                                    else _LAUNCH_STATE_COMPENSATING
                                ),
                                "last_error": failure,
                            },
                            "updated_at": utc_now(),
                        }
                    )
                )
                self._revoke_provider_join_credential(session, record)
                machine = machines.get_across_workspaces(prepared.machine.id)
                if machine is not None:
                    if terminated:
                        machines.upsert(
                            machine.model_copy(update={"status": ResourceStatus.Deleted})
                        )
                    elif created is None:
                        machines.upsert(
                            machine.model_copy(update={"status": ResourceStatus.Failed})
                        )

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
                                "tailnet_phase": TailnetEnrollmentPhase.Revoked,
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

    def _terminate_overdue_stale_machines(
        self,
        client: DirectMachineProvider,
        *,
        provider_name: str,
        pool: MachinePool,
        expected: set[str],
        stale: set[str],
        now: datetime,
    ) -> None:
        """Terminate provider machines unknown to durable state, after a grace window.

        Staleness is first observed, then enforced only once the machine has
        stayed stale for the provider's configured grace window, so machines
        created by a still-open launch transaction are never reaped mid-boot.
        First-seen tracking is process-local: a restart only delays reclaim by
        at most one grace window and can never terminate early.
        """
        for key in [
            key
            for key in self.stale_first_seen
            if key[0] == provider_name and key[1] == pool and key[2] not in stale
        ]:
            del self.stale_first_seen[key]
        if not stale:
            return
        grace = self.reclaim.stale_grace_for(provider_name)
        overdue: set[str] = set()
        for machine_id in stale:
            first_seen = self.stale_first_seen.setdefault(
                (provider_name, pool, machine_id),
                now,
            )
            if now - first_seen >= grace:
                overdue.add(machine_id)
        if not overdue:
            return
        result = client.reconcile_machines(
            pool,
            expected | (stale - overdue),
            terminate_stale=True,
        )
        for machine_id in result.terminated_machine_ids:
            self.stale_first_seen.pop((provider_name, pool, machine_id), None)
            LOGGER.warning(
                "terminated stale provider machine unknown to durable state",
                extra={
                    "provider": provider_name,
                    "pool": pool,
                    "machine_id": machine_id,
                },
            )

    def _provider_bootstrap_failure_to_reclaim(
        self,
        session: DatabaseSession,
        pool: ComputeUnitRecord,
        record: ComputeProviderInstanceRecord,
        *,
        now: datetime,
    ) -> MachineBootstrapFailureReason | None:
        deadline = self.reclaim.phase_deadline_for(record.provider, record.bootstrap_phase)
        if deadline is None:
            return None
        phase_started_at = _utc(record.bootstrap_observed_at or record.created_at)
        if now - phase_started_at < deadline:
            return None
        if record.bootstrap_phase is MachineBootstrapPhase.Failed:
            # The node named its own failure. Reclaim it under that reason rather
            # than re-diagnosing it as a timeout it did not have.
            return record.bootstrap_failure_reason or MachineBootstrapFailureReason.Unknown
        if record.bootstrap_phase in {
            MachineBootstrapPhase.Requested,
            MachineBootstrapPhase.Provisioning,
            MachineBootstrapPhase.Booting,
        }:
            return MachineBootstrapFailureReason.BootstrapTimedOut
        if record.machine_id is None:
            return MachineBootstrapFailureReason.BootstrapTimedOut
        enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
            pool.workspace_id,
            record.machine_id,
        )
        if enrollment is None:
            return MachineBootstrapFailureReason.BootstrapTimedOut
        if self.scheduler_hooks is None:
            msg = "provider bootstrap reclaim requires scheduler worker state"
            raise RuntimeError(msg)
        try:
            serves = machine_serves_workloads(
                enrollment,
                machine_id=record.machine_id,
                worker_state=self.scheduler_hooks,
            )
        except Exception:
            # This answer decides whether a billable machine is terminated. An
            # unreachable worker-state store is "unknown", and unknown machines
            # are kept, not reclaimed.
            LOGGER.exception(
                "worker state was unreachable while reclaiming machine %s; keeping it",
                record.machine_id,
            )
            return None
        return None if serves else MachineBootstrapFailureReason.WorkerReadinessFailed

    def _persist_zero_capacity_repair(
        self,
        unit_name: ComputeUnitRecord,
        *,
        maximum: int,
        observed: ProviderUnitSnapshot,
    ) -> ComputeUnitRecord:
        with self.context.database.session() as session:
            pools = ComputeUnitRepository(session)
            current = _require_internal_pooled_unit(
                pools.get_by_name(unit_name.workspace_id, unit_name.name, for_update=True),
                unit_name=unit_name.name,
            )
            if (
                current.capacity_owner_id != unit_name.capacity_owner_id
                or current.generation != unit_name.generation
                or current.desired_machines != 0
            ):
                raise ConflictError(
                    f"compute pool {unit_name.name!r} zero-capacity repair was superseded"
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
                    f"compute pool {unit_name.name!r} zero-capacity repair was superseded"
                )
            return intent

    @staticmethod
    def _schedule_provider_machine_identity_cleanup(
        session: DatabaseSession,
        enrollment: ComputeMachineEnrollmentRecord,
        *,
        now: datetime,
    ) -> None:
        auth_key_ids = _unique_nonempty(
            [enrollment.tailnet_auth_key_id, *enrollment.tailnet_cleanup_auth_key_ids]
        )
        device_ids = _unique_nonempty(
            [enrollment.tailnet_device_id, *enrollment.tailnet_cleanup_device_ids]
        )
        generations = list(range(1, enrollment.tailnet_generation + 1))
        if not generations and not auth_key_ids and not device_ids:
            return
        identity_expiry = enrollment.tailnet_auth_key_expires_at or now
        not_before = max(now, _utc(identity_expiry)) + timedelta(
            seconds=PROVIDER_MACHINE_IDENTITY_SETTLE_SECONDS
        )
        TailnetCleanupTombstoneRepository(session).schedule(
            workspace_id=enrollment.workspace_id,
            pool=enrollment.pool,
            machine_id=enrollment.machine_id,
            generations=generations,
            auth_key_ids=auth_key_ids,
            device_ids=device_ids,
            not_before=not_before,
            now=now,
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

    def _launch_intent_overdue(
        self,
        record: ComputeProviderInstanceRecord,
        *,
        now: datetime,
    ) -> bool:
        deadline = self.reclaim.launch_intent_settle_for(record.provider)
        return now - _utc(record.created_at) >= deadline

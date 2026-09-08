from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from database.repositories.compute import (
    AwsAccountConnectionRepository,
    AwsAuthorizationCleanupTombstoneRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceMemberRepository
from database.types import DatabaseSession
from observability.workspace_changes import WorkspaceChangePublisher
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountAuthorizationPlan,
    AwsAccountConnection,
    AwsAccountConnectionErrorCode,
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
    AwsAccountValidationResult,
    AwsAuthorizationCleanupStatus,
    AwsAuthorizationCleanupTombstone,
    AwsConnectionStackAction,
)
from shared.capacity import MachinePool
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, ComputeUnitPhase
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.aws_connections import (
    AwsConnectionCreateRequest,
    AwsConnectionReconnectRequest,
    AwsFleetEnsureRequest,
)
from shared.http.workspace_changes import WorkspaceChangeTopic, WorkspaceChangeType
from shared.timestamps import utc_now

from compute.bucket_access import AwsConnectionBucketAccessReconciler
from compute.context import ComputeContext


class AwsAccountAuthorizationPlanner(Protocol):
    def plan(
        self,
        *,
        user_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        network: AwsAccountNetwork | None,
    ) -> AwsAccountAuthorizationPlan: ...


class AwsAccountConnectionValidator(Protocol):
    def validate(
        self,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountValidationResult: ...


@dataclass(frozen=True, slots=True)
class AwsAccountRevocationAction:
    url: str | None
    label: str


@dataclass(frozen=True, slots=True)
class AwsAuthorizationCleanupResult:
    status: AwsAuthorizationCleanupStatus
    error_code: AwsAccountConnectionErrorCode | None = None
    error_message: str = ""
    customer_action: AwsAccountRevocationAction | None = None

    def __post_init__(self) -> None:
        if (self.error_code is None) != (not self.error_message):
            raise ValueError("AWS cleanup error code and message must be set together")
        if self.status is AwsAuthorizationCleanupStatus.ActionRequired and not self.error_message:
            raise ValueError("AWS action-required cleanup must explain the required action")
        if self.status is not AwsAuthorizationCleanupStatus.ActionRequired and (
            self.customer_action is not None
        ):
            raise ValueError("AWS customer action is only valid for action-required cleanup")


class AwsAccountAuthorizationLifecycle(Protocol):
    def reconcile_authorization_cleanup(
        self,
        *,
        account_id: str,
        external_id: str,
        authorization: AwsAccountAuthorizationGeneration,
        operation_id: str,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        remove_node_identity: bool,
    ) -> AwsAuthorizationCleanupResult: ...


@dataclass(frozen=True, slots=True)
class AwsAccountPoolDrain:
    total_pools: int
    remaining_pools: int


class AwsAccountPoolDrainer(Protocol):
    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace_ids: Sequence[str],
    ) -> AwsAccountPoolDrain: ...


class AwsAccountConnectionValidationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: AwsAccountConnectionErrorCode = AwsAccountConnectionErrorCode.UpstreamUnavailable,
    ) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionAuthorization:
    connection: AwsAccountConnection
    authorization_stack: AwsConnectionStackAction | None
    external_id: str | None


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionReconcileBatch:
    processed_count: int = 0
    completed_count: int = 0
    failure_count: int = 0


class AwsConnectionCapacityBaseline(Protocol):
    def reconcile_workspace_baseline(self, workspace_id: str) -> None: ...


class ConnectedCloudAdmission(Protocol):
    def assert_may_use_connected_cloud(self, session: DatabaseSession, *, user_id: str) -> None: ...


@dataclass(slots=True)
class AwsAccountConnectionService:
    context: ComputeContext
    authorization_planner: AwsAccountAuthorizationPlanner
    validator: AwsAccountConnectionValidator
    authorization_lifecycle: AwsAccountAuthorizationLifecycle
    pool_drainer: AwsAccountPoolDrainer
    admission: ConnectedCloudAdmission
    bucket_access_reconciler: AwsConnectionBucketAccessReconciler | None = None
    capacity_baseline: AwsConnectionCapacityBaseline | None = None
    workspace_changes: WorkspaceChangePublisher | None = None
    external_id_bytes: int = 48
    validation_lease_seconds: float = 300
    reconcile_claim_seconds: float = 120
    provider_poll_seconds: float = 10
    draft_ttl_seconds: float = 86400
    cleanup_tombstone_ttl_seconds: float = 604800
    maximum_backoff_seconds: float = 300
    cleanup_max_attempts: int = 120
    cleanup_timeout_seconds: float = 7200

    def connect(
        self,
        request: AwsConnectionCreateRequest | AwsFleetEnsureRequest,
        *,
        user_id: str,
        platform_fleet: bool = False,
    ) -> AwsAccountConnectionAuthorization:
        """Connect an AWS account, a customer's by default.

        `platform_fleet` says this is the platform's own account, which makes its
        machines serve every customer and bill to the fleet. It is not on the
        request contract: a customer who could set it would be declaring their own
        hardware to be ours. Only the administrator route passes it.
        """
        pool = (
            request.pool
            if isinstance(request, AwsConnectionCreateRequest) and not platform_fleet
            else MachinePool(LAZYCLOUD_MACHINE_POOL)
        )
        with self.context.database.session() as session:
            if not platform_fleet:
                self.admission.assert_may_use_connected_cloud(session, user_id=user_id)
            existing = AwsAccountConnectionRepository(session).get_for_user(user_id)
            if existing is not None:
                if self._matches_existing_draft(existing, request, pool=pool):
                    pending = existing.pending_authorization
                    if pending is None:
                        raise ConflictError("AWS account connection setup was superseded")
                    return self._authorization(existing, pending)
                raise ConflictError("this account already has an AWS account connection")

        connection_id = str(uuid4())
        # Supplied when the role already exists and already enforces one, minted
        # when this platform is the one creating the role.
        external_id = request.external_id or self._external_id()
        plan = self._plan(
            user_id=user_id,
            connection_id=connection_id,
            generation=1,
            account_id=request.account_id,
            external_id=external_id,
            role_arn=request.role_arn,
            active_authorization=None,
            node_role_arn=None,
            node_instance_profile_arn=None,
            network=request.network,
        )
        self._validate_plan_account(plan, request.account_id)
        now = utc_now()
        pending = self._pending_authorization(
            generation=1,
            plan=plan,
            now=now,
            expires_at=now + timedelta(seconds=self.draft_ttl_seconds),
        )
        connection = AwsAccountConnection(
            id=connection_id,
            user_id=user_id,
            platform_fleet=platform_fleet,
            pool=pool,
            account_id=request.account_id,
            external_id=external_id,
            phase=AwsAccountConnectionPhase.AwaitingAuthorization,
            pending_authorization=pending,
            node_role_arn=plan.node_role_arn,
            node_instance_profile_arn=plan.node_instance_profile_arn,
            network=plan.network,
            customer_action_url=None,
            customer_action_label=(
                "Create the connection stack" if plan.authorization_stack else ""
            ),
            next_reconcile_at=now,
            created_at=now,
            updated_at=now,
        )
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            if not platform_fleet:
                self.admission.assert_may_use_connected_cloud(session, user_id=user_id)
            if repository.get_for_user(user_id, for_update=True) is not None:
                raise ConflictError("this account already has an AWS account connection")
            connection = repository.create(connection)
        self._publish(connection, WorkspaceChangeType.Created)
        return self._authorization(connection, pending)

    def current(self, *, user_id: str) -> AwsAccountConnection | None:
        with self.context.database.session() as session:
            return AwsAccountConnectionRepository(session).get_for_user(user_id)

    def get(self, *, user_id: str) -> AwsAccountConnection:
        connection = self.current(user_id=user_id)
        if connection is None:
            raise NotFoundError("AWS account connection not found")
        return connection

    def ensure_fleet(self, request: AwsFleetEnsureRequest, *, user_id: str) -> AwsAccountConnection:
        """Register or verify the platform account's infrastructure."""
        if self.current(user_id=user_id) is None:
            try:
                self.connect(request, user_id=user_id, platform_fleet=True)
            except ConflictError:
                # Another ensure may have created the row. Validate it under lock below.
                if self.current(user_id=user_id) is None:
                    raise
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get_for_user(user_id, for_update=True)
            if current is None:
                raise ConflictError("Fleet connection was removed during ensure")
            authorizations = [
                authorization
                for authorization in (current.active_authorization, current.pending_authorization)
                if authorization is not None
            ]
            network = current.network
            if (
                not current.platform_fleet
                or current.account_id != request.account_id
                or current.pool != MachinePool(LAZYCLOUD_MACHINE_POOL)
                or not secrets.compare_digest(current.external_id, request.external_id)
                or not authorizations
                or any(
                    authorization.role_arn != request.role_arn for authorization in authorizations
                )
                or network is None
                or network.vpc_id != request.network.vpc_id
                or set(network.subnet_ids) != set(request.network.subnet_ids)
                or network.security_group_id != request.network.security_group_id
            ):
                raise ConflictError(
                    "Fleet infrastructure does not match the existing connection. "
                    "Reconcile account, role, external ID and network before deploying."
                )
            if current.phase not in {
                AwsAccountConnectionPhase.Ready,
                AwsAccountConnectionPhase.AwaitingAuthorization,
                AwsAccountConnectionPhase.Validating,
                AwsAccountConnectionPhase.Degraded,
            }:
                raise ConflictError("Fleet authorization transition must finish before deploying")
            return current

    def validate(self, *, user_id: str) -> AwsAccountConnection:
        started_at = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get_for_user(user_id, for_update=True)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            if not current.platform_fleet:
                self.admission.assert_may_use_connected_cloud(session, user_id=user_id)
            target = self._validation_target(current)
            validating = self._begin_validation(current, target, started_at)
            repository.save(validating)
        self._publish(validating, WorkspaceChangeType.Updated)

        try:
            result = self.validator.validate(
                validating, self._authorization_by_id_required(validating, target.id)
            )
            self._validate_result(validating, target, result)
        except AwsAccountConnectionValidationError as exc:
            failed = self._finish_validation_failure(
                validating,
                authorization_id=target.id,
                validation_generation=target.validation_generation + 1,
                code=exc.code,
                message=exc.message,
            )
            self._publish(failed, WorkspaceChangeType.Updated)
            return failed

        ready = self._finish_validation_success(
            validating,
            authorization_id=target.id,
            validation_generation=target.validation_generation + 1,
            result=result,
        )
        if ready.phase is AwsAccountConnectionPhase.Ready:
            self._apply_capacity_baseline(ready)
        self._publish(ready, WorkspaceChangeType.Updated)
        return ready

    def reconnect(
        self,
        request: AwsConnectionReconnectRequest,
        *,
        user_id: str,
    ) -> AwsAccountConnectionAuthorization:
        with self.context.database.session() as session:
            current = AwsAccountConnectionRepository(session).get_for_user(user_id)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            if not current.platform_fleet:
                self.admission.assert_may_use_connected_cloud(session, user_id=user_id)
            if current.phase is AwsAccountConnectionPhase.ReconnectPending:
                pending = current.pending_authorization
                if pending is None:
                    raise ConflictError("AWS authorization replacement was superseded")
                return self._authorization(current, pending)
            if current.phase not in {
                AwsAccountConnectionPhase.Ready,
                AwsAccountConnectionPhase.Degraded,
            }:
                raise ConflictError("AWS account connection cannot start authorization replacement")
            active = current.active_authorization
            if active is None:
                raise ConflictError("AWS account connection has no active authorization")
            reconnect_mode = (
                AwsAccountAuthorizationMode.ExistingRole
                if request.role_arn is not None
                else AwsAccountAuthorizationMode.ManagedStack
            )
            if reconnect_mode is not active.authorization_mode:
                raise ConflictError("AWS reconnect must preserve its authorization mode")
            if reconnect_mode is AwsAccountAuthorizationMode.ExistingRole and (
                request.role_arn != active.role_arn
            ):
                raise InvalidInputError(
                    "existing-role reconnect must revalidate the connected role"
                )

        generation = active.generation + 1
        plan = self._plan(
            user_id=user_id,
            connection_id=current.id,
            generation=generation,
            account_id=current.account_id,
            external_id=current.external_id,
            role_arn=request.role_arn,
            active_authorization=active,
            node_role_arn=current.node_role_arn,
            node_instance_profile_arn=current.node_instance_profile_arn,
        )
        self._validate_plan_account(plan, current.account_id)
        if (
            plan.node_role_arn != current.node_role_arn
            or plan.node_instance_profile_arn != current.node_instance_profile_arn
        ):
            raise UpstreamUnavailableError("AWS replacement changed the shared node IAM identity")
        now = utc_now()
        pending = self._pending_authorization(
            generation=generation,
            plan=plan,
            now=now,
            expires_at=now + timedelta(seconds=self.draft_ttl_seconds),
        )
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            durable = repository.get_for_user(user_id, for_update=True)
            if (
                durable is None
                or durable.revision != current.revision
                or durable.active_authorization is None
                or durable.active_authorization.id != active.id
            ):
                raise ConflictError("AWS account connection lifecycle was superseded")
            reconnecting = durable.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.ReconnectPending,
                    "pending_authorization": pending,
                    "customer_action_url": None,
                    "customer_action_label": (
                        "Create the connection stack" if plan.authorization_stack else ""
                    ),
                    "next_reconcile_at": now,
                    "reconcile_attempt_count": 0,
                    "last_error": "",
                    "revision": durable.revision + 1,
                    "updated_at": now,
                }
            )
            repository.save(reconnecting)
        self._publish(reconnecting, WorkspaceChangeType.Updated)
        return self._authorization(reconnecting, pending)

    def cancel_reconnect(self, *, user_id: str) -> AwsAccountConnection:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get_for_user(user_id, for_update=True)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            if current.phase is not AwsAccountConnectionPhase.ReconnectPending:
                if current.phase is AwsAccountConnectionPhase.Ready:
                    return current
                raise ConflictError("AWS account connection has no pending replacement")
            pending = current.pending_authorization
            active = current.active_authorization
            if pending is None or active is None:
                raise ConflictError("AWS authorization replacement state is incomplete")
            self._create_tombstone(
                session,
                current,
                pending,
                remove_node_identity=False,
                now=now,
            )
            ready = current.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.Ready,
                    "pending_authorization": None,
                    "next_reconcile_at": None,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "reconcile_attempt_count": 0,
                    "customer_action_url": None,
                    "customer_action_label": "",
                    "last_error": "",
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            repository.save(ready)
        self._publish(ready, WorkspaceChangeType.Updated)
        return ready

    def remove(self, *, user_id: str) -> AwsAccountConnection | None:
        now = utc_now()
        deleted: AwsAccountConnection | None = None
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get_for_user(user_id, for_update=True)
            if current is None:
                return None
            if current.active_authorization is None:
                if current.pending_authorization is not None:
                    self._create_tombstone(
                        session,
                        current,
                        current.pending_authorization,
                        remove_node_identity=True,
                        now=now,
                    )
                repository.delete(current)
                deleted = current
                result = None
            elif (
                current.phase
                in {
                    AwsAccountConnectionPhase.DisconnectDraining,
                    AwsAccountConnectionPhase.Revoking,
                    AwsAccountConnectionPhase.VerifyingRevocation,
                    AwsAccountConnectionPhase.ActionRequired,
                }
                and current.pending_authorization is None
                and current.retiring_authorization is None
            ):
                return current
            else:
                for authorization in (
                    current.pending_authorization,
                    current.retiring_authorization,
                ):
                    if authorization is not None:
                        self._create_tombstone(
                            session,
                            current,
                            authorization,
                            remove_node_identity=False,
                            now=now,
                        )
                result = current.model_copy(
                    update={
                        "phase": AwsAccountConnectionPhase.DisconnectDraining,
                        "pending_authorization": None,
                        "retiring_authorization": None,
                        "provider_operation_id": None,
                        "provider_operation_started_at": None,
                        "next_reconcile_at": now,
                        "claim_token": None,
                        "claim_expires_at": None,
                        "reconcile_attempt_count": 0,
                        "customer_action_url": None,
                        "customer_action_label": "",
                        "last_error": "",
                        "revision": current.revision + 1,
                        "updated_at": now,
                    }
                )
                repository.save(result)
        if deleted is not None:
            self._publish(deleted, WorkspaceChangeType.Deleted)
        elif result is not None:
            self._publish(result, WorkspaceChangeType.Updated)
        return result

    def retry(self, *, user_id: str) -> AwsAccountConnection:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get_for_user(user_id, for_update=True)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            if current.phase is AwsAccountConnectionPhase.ActionRequired:
                if current.retiring_authorization is not None:
                    phase = AwsAccountConnectionPhase.RetiringAuthorization
                elif current.active_authorization is not None:
                    phase = AwsAccountConnectionPhase.Revoking
                else:
                    raise ConflictError("AWS cleanup state has no authorization")
            elif current.phase is AwsAccountConnectionPhase.Degraded:
                if not current.platform_fleet:
                    self.admission.assert_may_use_connected_cloud(session, user_id=user_id)
                phase = AwsAccountConnectionPhase.Degraded
            else:
                return current
            retrying = current.model_copy(
                update={
                    "phase": phase,
                    "provider_operation_id": (
                        current.provider_operation_id or self._operation_id()
                        if phase
                        in {
                            AwsAccountConnectionPhase.Revoking,
                            AwsAccountConnectionPhase.RetiringAuthorization,
                        }
                        else current.provider_operation_id
                    ),
                    "provider_operation_started_at": (
                        now
                        if phase
                        in {
                            AwsAccountConnectionPhase.Revoking,
                            AwsAccountConnectionPhase.RetiringAuthorization,
                        }
                        else current.provider_operation_started_at
                    ),
                    "next_reconcile_at": now,
                    "reconcile_attempt_count": 0,
                    "customer_action_url": None,
                    "customer_action_label": "",
                    "last_error": "",
                    "revision": current.revision + 1,
                    "updated_at": now,
                }
            )
            repository.save(retrying)
        self._publish(retrying, WorkspaceChangeType.Updated)
        return retrying

    def reconcile_due(self, *, limit: int = 100) -> AwsAccountConnectionReconcileBatch:
        now = utc_now()
        lease_until = now + timedelta(seconds=self.reconcile_claim_seconds)
        with self.context.database.session() as session:
            connections = AwsAccountConnectionRepository(session).claim_due(
                now=now,
                lease_until=lease_until,
                limit=limit,
            )
            remaining = max(limit - len(connections), 0)
            tombstones = (
                AwsAuthorizationCleanupTombstoneRepository(session).claim_due(
                    now=now,
                    lease_until=lease_until,
                    limit=remaining,
                )
                if remaining
                else []
            )

        completed = 0
        failures = 0
        for connection in connections:
            try:
                if self._reconcile_connection(connection):
                    completed += 1
            except (ConflictError, UpstreamUnavailableError):
                failures += 1
        for tombstone in tombstones:
            try:
                if self._reconcile_tombstone(tombstone):
                    completed += 1
            except UpstreamUnavailableError:
                failures += 1
        return AwsAccountConnectionReconcileBatch(
            processed_count=len(connections) + len(tombstones),
            completed_count=completed,
            failure_count=failures,
        )

    def _reconcile_connection(self, claimed: AwsAccountConnection) -> bool:
        now = utc_now()
        if self._draft_expired(claimed, now):
            self._expire_draft(claimed, now)
            return True
        if claimed.bucket_access_reconcile_pending:
            if self.bucket_access_reconciler is None:
                return self._finish_claim_error(
                    claimed,
                    "AWS bucket access reconciliation is not configured",
                    now,
                )
            try:
                self.bucket_access_reconciler.reconcile_connection(claimed)
            except UpstreamUnavailableError as exc:
                return self._finish_claim_error(claimed, str(exc), now)
            claimed = claimed.model_copy(
                update={
                    "bucket_access_reconcile_pending": False,
                    "last_error": "",
                    "updated_at": now,
                }
            )
        if claimed.phase in {
            AwsAccountConnectionPhase.AwaitingAuthorization,
            AwsAccountConnectionPhase.Validating,
            AwsAccountConnectionPhase.Degraded,
            AwsAccountConnectionPhase.ReconnectPending,
        }:
            return self._reconcile_validation(claimed, now)
        if claimed.phase is AwsAccountConnectionPhase.DisconnectDraining:
            return self._reconcile_drain(claimed, now)
        if claimed.phase in {
            AwsAccountConnectionPhase.RetiringAuthorization,
            AwsAccountConnectionPhase.Revoking,
            AwsAccountConnectionPhase.VerifyingRevocation,
        }:
            return self._reconcile_cleanup(claimed, now)
        if claimed.phase is AwsAccountConnectionPhase.Ready:
            self._apply_capacity_baseline(claimed)
        return self._release_unchanged_claim(claimed, now)

    def _apply_capacity_baseline(self, connection: AwsAccountConnection) -> None:
        """Apply managed warm capacity to every workspace owned by the account."""
        if self.capacity_baseline is None:
            return
        for workspace_id in self._owned_workspace_ids(connection.user_id):
            self.capacity_baseline.reconcile_workspace_baseline(workspace_id)

    def _reconcile_validation(self, claimed: AwsAccountConnection, now: datetime) -> bool:
        target = claimed.pending_authorization or claimed.active_authorization
        if target is None:
            return self._finish_claim_error(
                claimed, "AWS validation state has no authorization", now
            )
        validation_generation = target.validation_generation + 1
        validating_target = target.model_copy(
            update={
                "phase": AwsAccountAuthorizationPhase.Validating,
                "validation_generation": validation_generation,
                "last_validation_started_at": now,
                "error_code": None,
                "error_message": "",
                "updated_at": now,
            }
        )
        validating = self._replace_authorization(claimed, target.id, validating_target)
        try:
            result = self.validator.validate(validating, validating_target)
            self._validate_result(validating, validating_target, result)
        except AwsAccountConnectionValidationError as exc:
            failed = self._validation_failure_model(
                validating,
                target=validating_target,
                code=exc.code,
                message=exc.message,
                now=now,
            )
            saved = self._finish_connection_claim(claimed, failed)
            return saved is not None and saved.next_reconcile_at is None
        ready = self._validation_success_model(
            validating,
            target=validating_target,
            result=result,
        )
        saved = self._finish_connection_claim(claimed, ready)
        if saved is not None and saved.phase is AwsAccountConnectionPhase.Ready:
            self._apply_capacity_baseline(saved)
        return saved is not None and saved.next_reconcile_at is None

    def _reconcile_drain(self, claimed: AwsAccountConnection, now: datetime) -> bool:
        try:
            drain = self.pool_drainer.request_connection_drain(
                claimed.id,
                workspace_ids=self._owned_workspace_ids(claimed.user_id),
            )
        except (ConflictError, UpstreamUnavailableError) as exc:
            return self._finish_claim_error(claimed, str(exc), now)
        phase = AwsAccountConnectionPhase.DisconnectDraining
        operation_id = claimed.provider_operation_id
        operation_started_at = claimed.provider_operation_started_at
        next_reconcile_at = now + timedelta(seconds=self.provider_poll_seconds)
        if drain.remaining_pools == 0:
            phase = AwsAccountConnectionPhase.Revoking
            operation_id = operation_id or self._operation_id()
            operation_started_at = operation_started_at or now
            next_reconcile_at = now
        updated = claimed.model_copy(
            update={
                "phase": phase,
                "provider_operation_id": operation_id,
                "provider_operation_started_at": operation_started_at,
                "drain_total_pools": drain.total_pools,
                "drain_remaining_pools": drain.remaining_pools,
                "next_reconcile_at": next_reconcile_at,
                "reconcile_attempt_count": 0,
                "last_error": "",
                "updated_at": now,
            }
        )
        saved = self._finish_connection_claim(claimed, updated)
        return saved is not None and saved.next_reconcile_at is None

    def _reconcile_cleanup(self, claimed: AwsAccountConnection, now: datetime) -> bool:
        retirement = claimed.phase is AwsAccountConnectionPhase.RetiringAuthorization
        authorization = (
            claimed.retiring_authorization if retirement else claimed.active_authorization
        )
        if authorization is None or claimed.provider_operation_id is None:
            return self._finish_claim_error(claimed, "AWS cleanup state is incomplete", now)
        try:
            result = self.authorization_lifecycle.reconcile_authorization_cleanup(
                account_id=claimed.account_id,
                external_id=claimed.external_id,
                authorization=authorization,
                operation_id=claimed.provider_operation_id,
                node_role_arn=claimed.node_role_arn,
                node_instance_profile_arn=claimed.node_instance_profile_arn,
                remove_node_identity=not retirement,
            )
        except AwsAccountConnectionValidationError as exc:
            return self._finish_cleanup_error(claimed, exc.message, now)
        if result.status is AwsAuthorizationCleanupStatus.Complete:
            if retirement:
                ready = claimed.model_copy(
                    update={
                        "phase": AwsAccountConnectionPhase.Ready,
                        "retiring_authorization": None,
                        "provider_operation_id": None,
                        "provider_operation_started_at": None,
                        "next_reconcile_at": None,
                        "reconcile_attempt_count": 0,
                        "customer_action_url": None,
                        "customer_action_label": "",
                        "last_error": "",
                        "updated_at": now,
                    }
                )
                saved = self._finish_connection_claim(claimed, ready)
                return saved is not None and saved.next_reconcile_at is None
            return self._complete_connection_cleanup(claimed)
        if result.status is AwsAuthorizationCleanupStatus.ActionRequired:
            action = result.customer_action
            action_required = claimed.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.ActionRequired,
                    "next_reconcile_at": None,
                    "customer_action_url": action.url if action is not None else None,
                    "customer_action_label": action.label if action is not None else "",
                    "last_error": result.error_message,
                    "updated_at": now,
                }
            )
            saved = self._finish_connection_claim(claimed, action_required)
            return saved is not None and saved.next_reconcile_at is None
        phase = claimed.phase
        if not retirement and result.status is AwsAuthorizationCleanupStatus.Verifying:
            phase = AwsAccountConnectionPhase.VerifyingRevocation
        attempts = claimed.reconcile_attempt_count + 1
        if self._cleanup_exhausted(claimed, attempts=attempts, now=now):
            return self._finish_cleanup_action_required(
                claimed,
                "AWS cleanup did not complete within the automatic retry window.",
                now,
            )
        updated = claimed.model_copy(
            update={
                "phase": phase,
                "next_reconcile_at": now + self._cleanup_backoff(attempts),
                "reconcile_attempt_count": attempts,
                "last_error": "",
                "updated_at": now,
            }
        )
        saved = self._finish_connection_claim(claimed, updated)
        return saved is not None and saved.next_reconcile_at is None

    def _reconcile_tombstone(self, claimed: AwsAuthorizationCleanupTombstone) -> bool:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAuthorizationCleanupTombstoneRepository(session)
            if claimed.expires_at <= now:
                return repository.complete(claimed)
        try:
            result = self.authorization_lifecycle.reconcile_authorization_cleanup(
                account_id=claimed.account_id,
                external_id=claimed.external_id,
                authorization=claimed.authorization,
                operation_id=claimed.provider_operation_id,
                node_role_arn=claimed.node_role_arn,
                node_instance_profile_arn=claimed.node_instance_profile_arn,
                remove_node_identity=claimed.remove_node_identity,
            )
        except AwsAccountConnectionValidationError as exc:
            updated = claimed.model_copy(
                update={
                    "next_reconcile_at": now
                    + self._cleanup_backoff(claimed.reconcile_attempt_count + 1),
                    "reconcile_attempt_count": claimed.reconcile_attempt_count + 1,
                    "last_error": exc.message,
                    "updated_at": now,
                }
            )
            with self.context.database.session() as session:
                saved = AwsAuthorizationCleanupTombstoneRepository(session).finish_claim(
                    claimed, updated
                )
            if saved is None:
                raise UpstreamUnavailableError("AWS cleanup tombstone claim expired") from exc
            return False
        with self.context.database.session() as session:
            repository = AwsAuthorizationCleanupTombstoneRepository(session)
            if result.status is AwsAuthorizationCleanupStatus.Complete:
                return repository.complete(claimed)
            status = result.status
            attempts = claimed.reconcile_attempt_count + 1
            updated = claimed.model_copy(
                update={
                    "status": status,
                    "next_reconcile_at": now + self._cleanup_backoff(attempts),
                    "reconcile_attempt_count": attempts,
                    "last_error": result.error_message,
                    "updated_at": now,
                }
            )
            return repository.finish_claim(claimed, updated) is not None

    def _finish_connection_claim(
        self,
        claimed: AwsAccountConnection,
        updated: AwsAccountConnection,
    ) -> AwsAccountConnection | None:
        with self.context.database.session() as session:
            saved = AwsAccountConnectionRepository(session).finish_claim(claimed, updated)
        if saved is None:
            return None
        self._publish(saved, WorkspaceChangeType.Updated)
        return saved

    def _finish_claim_error(
        self,
        claimed: AwsAccountConnection,
        message: str,
        now: datetime,
    ) -> bool:
        attempts = claimed.reconcile_attempt_count + 1
        updated = claimed.model_copy(
            update={
                "next_reconcile_at": now + self._backoff(attempts),
                "reconcile_attempt_count": attempts,
                "last_error": message,
                "updated_at": now,
            }
        )
        if self._finish_connection_claim(claimed, updated) is None:
            raise UpstreamUnavailableError(message)
        return False

    def _finish_cleanup_error(
        self,
        claimed: AwsAccountConnection,
        message: str,
        now: datetime,
    ) -> bool:
        attempts = claimed.reconcile_attempt_count + 1
        if self._cleanup_exhausted(claimed, attempts=attempts, now=now):
            return self._finish_cleanup_action_required(claimed, message, now)
        updated = claimed.model_copy(
            update={
                "next_reconcile_at": now + self._cleanup_backoff(attempts),
                "reconcile_attempt_count": attempts,
                "last_error": message,
                "updated_at": now,
            }
        )
        if self._finish_connection_claim(claimed, updated) is None:
            raise UpstreamUnavailableError(message)
        return False

    def _finish_cleanup_action_required(
        self,
        claimed: AwsAccountConnection,
        message: str,
        now: datetime,
    ) -> bool:
        updated = claimed.model_copy(
            update={
                "phase": AwsAccountConnectionPhase.ActionRequired,
                "next_reconcile_at": None,
                "customer_action_url": None,
                "customer_action_label": "Review AWS cleanup",
                "last_error": message,
                "updated_at": now,
            }
        )
        saved = self._finish_connection_claim(claimed, updated)
        return saved is not None and saved.next_reconcile_at is None

    def _release_unchanged_claim(self, claimed: AwsAccountConnection, now: datetime) -> bool:
        updated = claimed.model_copy(update={"next_reconcile_at": None, "updated_at": now})
        saved = self._finish_connection_claim(claimed, updated)
        return saved is not None and saved.next_reconcile_at is None

    def _complete_connection_cleanup(self, claimed: AwsAccountConnection) -> bool:
        with self.context.database.session() as session:
            connections = AwsAccountConnectionRepository(session)
            pools = ComputeUnitRepository(session)
            dependent = pools.list_for_provider_connection(claimed.id)
            if any(pool.phase is not ComputeUnitPhase.Deleted for pool in dependent):
                raise ConflictError("AWS account connection still has active compute pools")
            for pool in dependent:
                pools.records.delete(pool.id, workspace_id=pool.workspace_id)
            deleted = connections.delete_claimed(claimed)
        if deleted:
            self._publish(claimed, WorkspaceChangeType.Deleted)
        return deleted

    def _expire_draft(self, claimed: AwsAccountConnection, now: datetime) -> None:
        active = claimed.active_authorization
        pending = claimed.pending_authorization
        if pending is None:
            self._release_unchanged_claim(claimed, now)
            return
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            self._create_tombstone(
                session,
                claimed,
                pending,
                remove_node_identity=active is None,
                now=now,
            )
            if active is None:
                deleted = repository.delete_claimed(claimed)
                ready = None
            else:
                ready_model = claimed.model_copy(
                    update={
                        "phase": AwsAccountConnectionPhase.Ready,
                        "pending_authorization": None,
                        "next_reconcile_at": None,
                        "last_error": "",
                        "updated_at": now,
                    }
                )
                ready = repository.finish_claim(claimed, ready_model)
                deleted = False
        if deleted:
            self._publish(claimed, WorkspaceChangeType.Deleted)
        elif ready is not None:
            self._publish(ready, WorkspaceChangeType.Updated)

    def _draft_expired(self, connection: AwsAccountConnection, now: datetime) -> bool:
        pending = connection.pending_authorization
        return pending is not None and pending.expires_at is not None and pending.expires_at <= now

    def _begin_validation(
        self,
        current: AwsAccountConnection,
        target: AwsAccountAuthorizationGeneration,
        now: datetime,
    ) -> AwsAccountConnection:
        if (
            target.phase is AwsAccountAuthorizationPhase.Validating
            and target.last_validation_started_at is not None
            and (now - target.last_validation_started_at).total_seconds()
            < self.validation_lease_seconds
        ):
            raise ConflictError("AWS account connection validation is already running")
        validating_target = target.model_copy(
            update={
                "phase": AwsAccountAuthorizationPhase.Validating,
                "validation_generation": target.validation_generation + 1,
                "last_validation_started_at": now,
                "error_code": None,
                "error_message": "",
                "updated_at": now,
            }
        )
        connection = self._replace_authorization(current, target.id, validating_target)
        phase = (
            AwsAccountConnectionPhase.ReconnectPending
            if current.pending_authorization is not None
            and current.active_authorization is not None
            else AwsAccountConnectionPhase.Validating
        )
        return connection.model_copy(
            update={
                "phase": phase,
                "next_reconcile_at": now + timedelta(seconds=self.validation_lease_seconds),
                "claim_token": None,
                "claim_expires_at": None,
                "last_error": "",
                "revision": connection.revision + 1,
                "updated_at": now,
            }
        )

    def _finish_validation_success(
        self,
        started: AwsAccountConnection,
        *,
        authorization_id: str,
        validation_generation: int,
        result: AwsAccountValidationResult,
    ) -> AwsAccountConnection:
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get(started.id, for_update=True)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            target = self._matching_validation(current, authorization_id, validation_generation)
            ready = self._validation_success_model(current, target=target, result=result)
            ready = ready.model_copy(update={"revision": current.revision + 1})
            repository.save(ready)
            return ready

    def _finish_validation_failure(
        self,
        started: AwsAccountConnection,
        *,
        authorization_id: str,
        validation_generation: int,
        code: AwsAccountConnectionErrorCode,
        message: str,
    ) -> AwsAccountConnection:
        now = utc_now()
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            current = repository.get(started.id, for_update=True)
            if current is None:
                raise NotFoundError("AWS account connection not found")
            target = self._matching_validation(current, authorization_id, validation_generation)
            failed = self._validation_failure_model(
                current,
                target=target,
                code=code,
                message=message,
                now=now,
            ).model_copy(update={"revision": current.revision + 1})
            repository.save(failed)
            return failed

    def _validation_success_model(
        self,
        connection: AwsAccountConnection,
        *,
        target: AwsAccountAuthorizationGeneration,
        result: AwsAccountValidationResult,
    ) -> AwsAccountConnection:
        ready_target = target.model_copy(
            update={
                "managed_authorization": result.managed_authorization
                or target.managed_authorization,
                "authorization_stack": None,
                "phase": AwsAccountAuthorizationPhase.Ready,
                "last_validated_at": result.validated_at,
                "error_code": None,
                "error_message": "",
                "updated_at": result.validated_at,
            }
        )
        if connection.pending_authorization is not None and (
            connection.pending_authorization.id == target.id
        ):
            predecessor = connection.active_authorization
            if predecessor is None:
                return connection.model_copy(
                    update={
                        "phase": AwsAccountConnectionPhase.Ready,
                        "active_authorization": ready_target,
                        "pending_authorization": None,
                        "node_role_arn": result.node_role_arn,
                        "node_instance_profile_arn": result.node_instance_profile_arn,
                        "network": result.network or connection.network,
                        "next_reconcile_at": None,
                        "reconcile_attempt_count": 0,
                        "customer_action_url": None,
                        "customer_action_label": "",
                        "last_error": "",
                        "updated_at": result.validated_at,
                    }
                )
            return connection.model_copy(
                update={
                    "phase": AwsAccountConnectionPhase.RetiringAuthorization,
                    "active_authorization": ready_target,
                    "pending_authorization": None,
                    "retiring_authorization": predecessor.model_copy(
                        update={
                            "phase": AwsAccountAuthorizationPhase.Retiring,
                            "updated_at": result.validated_at,
                        }
                    ),
                    "node_role_arn": result.node_role_arn,
                    "node_instance_profile_arn": result.node_instance_profile_arn,
                    "network": result.network or connection.network,
                    "provider_operation_id": self._operation_id(),
                    "provider_operation_started_at": result.validated_at,
                    "next_reconcile_at": result.validated_at,
                    "reconcile_attempt_count": 0,
                    "customer_action_url": None,
                    "customer_action_label": "",
                    "last_error": "",
                    "updated_at": result.validated_at,
                }
            )
        return connection.model_copy(
            update={
                "phase": AwsAccountConnectionPhase.Ready,
                "active_authorization": ready_target,
                "node_role_arn": result.node_role_arn,
                "node_instance_profile_arn": result.node_instance_profile_arn,
                "network": result.network or connection.network,
                "next_reconcile_at": None,
                "reconcile_attempt_count": 0,
                "customer_action_url": None,
                "customer_action_label": "",
                "last_error": "",
                "updated_at": result.validated_at,
            }
        )

    def _validation_failure_model(
        self,
        connection: AwsAccountConnection,
        *,
        target: AwsAccountAuthorizationGeneration,
        code: AwsAccountConnectionErrorCode,
        message: str,
        now: datetime,
    ) -> AwsAccountConnection:
        degraded_target = target.model_copy(
            update={
                "phase": AwsAccountAuthorizationPhase.Degraded,
                "error_code": code,
                "error_message": message,
                "updated_at": now,
            }
        )
        degraded = self._replace_authorization(connection, target.id, degraded_target)
        attempts = connection.reconcile_attempt_count + 1
        phase = AwsAccountConnectionPhase.Degraded
        if (
            connection.active_authorization is not None
            and connection.pending_authorization is not None
            and connection.pending_authorization.id == target.id
        ):
            phase = AwsAccountConnectionPhase.ReconnectPending
        elif (
            connection.active_authorization is None
            and connection.pending_authorization is not None
            and code
            in {
                AwsAccountConnectionErrorCode.AssumeRoleDenied,
                AwsAccountConnectionErrorCode.UpstreamUnavailable,
            }
        ):
            phase = AwsAccountConnectionPhase.AwaitingAuthorization
        return degraded.model_copy(
            update={
                "phase": phase,
                "next_reconcile_at": now + self._backoff(attempts),
                "reconcile_attempt_count": attempts,
                "last_error": message,
                "updated_at": now,
            }
        )

    def _create_tombstone(
        self,
        session: DatabaseSession,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
        *,
        remove_node_identity: bool,
        now: datetime,
    ) -> AwsAuthorizationCleanupTombstone:
        tombstone = AwsAuthorizationCleanupTombstone(
            id=str(uuid4()),
            user_id=connection.user_id,
            connection_id=connection.id,
            account_id=connection.account_id,
            external_id=connection.external_id,
            authorization=authorization,
            node_role_arn=connection.node_role_arn,
            node_instance_profile_arn=connection.node_instance_profile_arn,
            remove_node_identity=remove_node_identity,
            provider_operation_id=self._operation_id(),
            next_reconcile_at=now,
            expires_at=now + timedelta(seconds=self.cleanup_tombstone_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        return AwsAuthorizationCleanupTombstoneRepository(session).create(tombstone)

    def _plan(
        self,
        *,
        user_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        network: AwsAccountNetwork | None = None,
    ) -> AwsAccountAuthorizationPlan:
        try:
            return self.authorization_planner.plan(
                user_id=user_id,
                connection_id=connection_id,
                generation=generation,
                account_id=account_id,
                external_id=external_id,
                role_arn=role_arn,
                active_authorization=active_authorization,
                node_role_arn=node_role_arn,
                node_instance_profile_arn=node_instance_profile_arn,
                network=network,
            )
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except AwsAccountConnectionValidationError as exc:
            raise UpstreamUnavailableError(exc.message) from exc

    def _owned_workspace_ids(self, user_id: str) -> list[str]:
        with self.context.database.session() as session:
            return WorkspaceMemberRepository(session).owned_workspace_ids(user_id)

    def _publish(self, connection: AwsAccountConnection, change: WorkspaceChangeType) -> None:
        """Announce the change to every workspace the connection backs.

        The dashboard subscribes per workspace, so an account-level change has to
        reach each of them or a workspace watching its own compute would never see
        that the account behind it moved.
        """
        if self.workspace_changes is None:
            return
        for workspace_id in self._owned_workspace_ids(connection.user_id):
            self.workspace_changes.emit_change(
                workspace_id=workspace_id,
                topic=WorkspaceChangeTopic.ComputeConnections,
                change=change,
                resource_id=connection.id,
            )

    def _backoff(self, attempt: int) -> timedelta:
        seconds = min(1 << max(attempt - 1, 0), self.maximum_backoff_seconds)
        return timedelta(seconds=seconds)

    def _cleanup_backoff(self, attempt: int) -> timedelta:
        seconds = min(
            self.provider_poll_seconds * (1 << min(max(attempt - 1, 0), 4)),
            self.maximum_backoff_seconds,
        )
        return timedelta(seconds=seconds)

    def _cleanup_exhausted(
        self,
        connection: AwsAccountConnection,
        *,
        attempts: int,
        now: datetime,
    ) -> bool:
        started_at = connection.provider_operation_started_at
        return attempts >= self.cleanup_max_attempts or (
            started_at is not None
            and (now - started_at).total_seconds() >= self.cleanup_timeout_seconds
        )

    def _external_id(self) -> str:
        return secrets.token_urlsafe(max(self.external_id_bytes, 32))

    @staticmethod
    def _operation_id() -> str:
        return f"cleanup-{uuid4().hex}"

    def _pending_authorization(
        self,
        *,
        generation: int,
        plan: AwsAccountAuthorizationPlan,
        now: datetime,
        expires_at: datetime,
    ) -> AwsAccountAuthorizationGeneration:
        return AwsAccountAuthorizationGeneration(
            id=str(uuid4()),
            generation=generation,
            role_arn=plan.role_arn,
            authorization_mode=plan.authorization_mode,
            managed_authorization=(
                plan.managed_authorization.model_copy(update={"generation": generation})
                if plan.managed_authorization is not None
                else None
            ),
            authorization_stack=plan.authorization_stack,
            phase=AwsAccountAuthorizationPhase.AwaitingAuthorization,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _authorization(
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountConnectionAuthorization:
        return AwsAccountConnectionAuthorization(
            connection=connection,
            authorization_stack=authorization.authorization_stack,
            external_id=(
                connection.external_id
                if authorization.authorization_mode is AwsAccountAuthorizationMode.ExistingRole
                else None
            ),
        )

    @staticmethod
    def _authorization_by_id(
        connection: AwsAccountConnection,
        authorization_id: str,
    ) -> AwsAccountAuthorizationGeneration | None:
        return next(
            (
                item
                for item in (
                    connection.active_authorization,
                    connection.pending_authorization,
                    connection.retiring_authorization,
                )
                if item is not None and item.id == authorization_id
            ),
            None,
        )

    def _authorization_by_id_required(
        self,
        connection: AwsAccountConnection,
        authorization_id: str,
    ) -> AwsAccountAuthorizationGeneration:
        authorization = self._authorization_by_id(connection, authorization_id)
        if authorization is None:
            raise ConflictError("AWS account authorization was superseded")
        return authorization

    @staticmethod
    def _replace_authorization(
        connection: AwsAccountConnection,
        authorization_id: str,
        replacement: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountConnection:
        if (
            connection.active_authorization is not None
            and connection.active_authorization.id == authorization_id
        ):
            return connection.model_copy(update={"active_authorization": replacement})
        if (
            connection.pending_authorization is not None
            and connection.pending_authorization.id == authorization_id
        ):
            return connection.model_copy(update={"pending_authorization": replacement})
        if (
            connection.retiring_authorization is not None
            and connection.retiring_authorization.id == authorization_id
        ):
            return connection.model_copy(update={"retiring_authorization": replacement})
        raise ConflictError("AWS account authorization was superseded")

    @staticmethod
    def _matching_validation(
        connection: AwsAccountConnection,
        authorization_id: str,
        validation_generation: int,
    ) -> AwsAccountAuthorizationGeneration:
        target = AwsAccountConnectionService._authorization_by_id(connection, authorization_id)
        if target is None or target.validation_generation != validation_generation:
            raise ConflictError("AWS account connection validation was superseded")
        return target

    @staticmethod
    def _validation_target(
        connection: AwsAccountConnection,
    ) -> AwsAccountAuthorizationGeneration:
        if connection.phase in {
            AwsAccountConnectionPhase.DisconnectDraining,
            AwsAccountConnectionPhase.Revoking,
            AwsAccountConnectionPhase.VerifyingRevocation,
            AwsAccountConnectionPhase.ActionRequired,
        }:
            raise ConflictError("AWS account connection is being removed")
        target = connection.pending_authorization or connection.active_authorization
        if target is None or target.phase is AwsAccountAuthorizationPhase.Retiring:
            raise ConflictError("AWS account connection has no authorization to validate")
        return target

    @staticmethod
    def _validate_plan_account(plan: AwsAccountAuthorizationPlan, account_id: str) -> None:
        if plan.role_arn.split(":", maxsplit=5)[4] != account_id:
            raise UpstreamUnavailableError("AWS authorization plan returned another account")

    @staticmethod
    def _validate_result(
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
        result: AwsAccountValidationResult,
    ) -> None:
        if result.account_id != connection.account_id or result.role_arn != authorization.role_arn:
            raise AwsAccountConnectionValidationError(
                "AWS assumed-role identity does not match the connected account and role",
                code=AwsAccountConnectionErrorCode.AccountMismatch,
            )

    @staticmethod
    def _matches_existing_draft(
        existing: AwsAccountConnection,
        request: AwsConnectionCreateRequest | AwsFleetEnsureRequest,
        *,
        pool: MachinePool,
    ) -> bool:
        pending = existing.pending_authorization
        mode = (
            AwsAccountAuthorizationMode.ExistingRole
            if request.role_arn is not None
            else AwsAccountAuthorizationMode.ManagedStack
        )
        return (
            existing.account_id == request.account_id
            and existing.pool == pool
            and existing.active_authorization is None
            and pending is not None
            and pending.authorization_mode is mode
            and (request.role_arn is None or pending.role_arn == request.role_arn)
        )


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionDirectory:
    context: ComputeContext

    def current(self, *, user_id: str) -> AwsAccountConnection | None:
        with self.context.database.session() as session:
            return AwsAccountConnectionRepository(session).get_for_user(user_id)

    def list_for_workspace(self, workspace_id: str) -> tuple[AwsAccountConnection, ...]:
        """Connections a workspace may place work on, reached through its owner.

        Placement asks per workspace and always did; what changed is that the answer
        now comes from the account behind it rather than the workspace itself.
        """
        with self.context.database.session() as session:
            repository = AwsAccountConnectionRepository(session)
            connection = repository.get_for_workspace_owner(workspace_id)
            fleet = [item for item in repository.list_all() if item.platform_fleet]
        by_id = {item.id: item for item in fleet}
        if connection is not None:
            by_id[connection.id] = connection
        return tuple(by_id[key] for key in sorted(by_id))

    def capacity_workspace(self, connection: AwsAccountConnection) -> str:
        with self.context.database.session() as session:
            workspace_ids = WorkspaceMemberRepository(session).owned_workspace_ids(
                connection.user_id
            )
        if not workspace_ids:
            raise RuntimeError(f"AWS provider {connection.id} has no capacity workspace")
        return min(workspace_ids)


__all__ = [
    "AwsAccountAuthorizationLifecycle",
    "AwsAccountAuthorizationPlanner",
    "AwsAccountConnectionAuthorization",
    "AwsAccountConnectionDirectory",
    "AwsAccountConnectionReconcileBatch",
    "AwsAccountConnectionService",
    "AwsAccountConnectionValidationError",
    "AwsAccountConnectionValidator",
    "AwsAccountPoolDrain",
    "AwsAccountPoolDrainer",
    "AwsAccountRevocationAction",
    "AwsAuthorizationCleanupResult",
]

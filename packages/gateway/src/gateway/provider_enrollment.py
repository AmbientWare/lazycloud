from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from secrets import token_urlsafe

from compute.agent_control import ComputePrincipal, plan_join_token_creation
from compute.offers import ReservationStatus
from compute.provider_launches import ProviderNodeLaunchService
from compute.provider_nodes import ProviderNodeIdentityProof, ProviderNodeIdentityVerifier
from compute.service import ComputeService
from coordination.rate_limit import release_slot, try_acquire_slot, try_consume
from coordination.redis_client import RedisClient
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeJoinCredentialRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import WorkspaceMemberRepository, WorkspaceRepository
from database.types import DatabaseSession
from provider_aws.provider_node_identity import AWS_STS_PROOF_TIMEOUT_SECONDS
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
)
from shared.compute_enrollment import MachineBootstrapPhase
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
)
from shared.errors import ConflictError, InvalidInputError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapFailureResponse,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeEnrollmentRequest,
)
from shared.provider_config import ProviderKind

from gateway.agent_enrollment import AgentJoinResult
from gateway.events import GatewayEventSink
from gateway.http import JoinAgentRequest, JoinAgentResponse
from gateway.service import GatewayControlService

# A degraded pool still enrolls and still accepts bootstrap reports. Refusing a
# healthy machine because earlier machines failed is self-reinforcing: the
# refusal is itself recorded as another bootstrap failure, so the pool can never
# recover without an operator, and the reports that would explain the original
# failure are discarded exactly when they matter most.
_ENROLLABLE_UNIT_PHASES = {
    ComputeUnitPhase.Provisioning,
    ComputeUnitPhase.Ready,
    ComputeUnitPhase.Updating,
    ComputeUnitPhase.Degraded,
}


_CONTROL_CHARACTERS = {chr(i) for i in range(32)} - {"\n", "\t"}
_INVENTORY_REFRESH_WINDOW_SECONDS = 5
_PROOF_SLOT_TTL_SECONDS = int(AWS_STS_PROOF_TIMEOUT_SECONDS) + 1


def _sanitized_excerpt(excerpt: str) -> str:
    return "".join(ch for ch in excerpt if ch not in _CONTROL_CHARACTERS)[:8192]


@dataclass(frozen=True, slots=True)
class ProviderNodeEnrollmentService:
    gateway: GatewayControlService
    compute: ComputeService
    identity_verifier: ProviderNodeIdentityVerifier
    launches: ProviderNodeLaunchService | None = None
    events: GatewayEventSink | None = None
    rate_limiter: RedisClient | None = None
    proof_max_inflight: int = 8
    client_ip_header: str = ""

    def enroll(
        self,
        request: ProviderNodeEnrollmentRequest,
        *,
        peer_address: str = "",
    ) -> JoinAgentResponse:
        pool, connection = self._enrollment_target(request)
        self._authorize_launch(request, pool)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
            peer_address=peer_address,
            launch_id=request.launch_id,
        )
        with self.gateway.services.context.database.session() as session:
            current, _ = self._enrollment_target_in_transaction(session, request)
            if (
                current.provider_ref != pool.provider_ref
                or current.workspace_id != pool.workspace_id
                or current.generation != pool.generation
                or current.provider_state.resource_id != pool.provider_state.resource_id
            ):
                raise ConflictError("provider node enrollment request changed")
            pool = current
            lease = (
                self._require_launches().lock_enrollment(
                    session, request, pool, machine_fingerprint=request.machine_fingerprint
                )
                if request.provider is not ProviderKind.Aws
                else None
            )
            result = None
            if lease is not None:
                result = self.gateway.resume_provider_agent_in_transaction(
                    session,
                    node_agent_token=SecretStr(request.node_agent_token),
                    pool=pool,
                    machine_fingerprint=request.machine_fingerprint,
                )
                if result is None and lease.enrolled:
                    raise InvalidInputError("provider node enrollment is no longer active")
            if result is None:
                result = self._join_verified_node(session, request, pool)
            self._finish_enrollment(session, request, pool, result.response)
            if lease is not None:
                lease.complete()
        self.gateway.publish_agent_join(result)
        return result.response

    def _join_verified_node(
        self,
        session: DatabaseSession,
        request: ProviderNodeEnrollmentRequest,
        pool: ComputeUnitRecord,
    ) -> AgentJoinResult:
        join_token = self._issue_join_token(
            session,
            pool.id,
            pool.workspace_id,
            pool.pool,
            pool.capacity_owner_id,
            owner_user_id=self._pool_owner(session, pool),
        )
        join_request = JoinAgentRequest(
            join_token=join_token.get_secret_value(),
            machine_fingerprint=request.machine_fingerprint,
            hostname=request.hostname,
            os=request.os,
            arch=request.arch,
            cpu_count=request.capacity.cpu_count,
            cpu_millicores=request.capacity.cpu_millicores,
            memory_mb=request.capacity.memory_mb,
            gpu=request.capacity.gpu,
            gpu_ids=request.capacity.gpu_ids,
            gpu_count=request.capacity.gpu_count,
            preflight=request.preflight,
            schedulable=request.requested_schedulable,
            executor=request.executor,
        )
        return self.gateway.join_agent_in_transaction(
            session,
            join_request,
            node_agent_token=(
                SecretStr(request.node_agent_token)
                if request.provider is not ProviderKind.Aws
                else None
            ),
        )

    def _finish_enrollment(
        self,
        session: DatabaseSession,
        request: ProviderNodeEnrollmentRequest,
        pool: ComputeUnitRecord,
        joined: JoinAgentResponse,
    ) -> None:
        if joined.workspace_id != pool.workspace_id or joined.pool != pool.pool:
            raise ConflictError("provider node joined a different compute pool")
        instances = ComputeProviderInstanceRepository(session)
        instance = instances.get_for_pool_instance(
            pool.id, request.provider_instance_id, for_update=True
        )
        if instance is None or instance.status not in {
            ReservationStatus.Pending,
            ReservationStatus.Active,
        }:
            raise ConflictError("provider node is no longer available for enrollment")
        bound = instances.bind_machine(pool.id, request.provider_instance_id, joined.machine_id)
        if bound is None:
            raise ConflictError("provider node is no longer available for enrollment")
        self.compute.record_provider_bootstrap_status_in_transaction(
            session,
            pool_id=pool.id,
            provider_instance_id=request.provider_instance_id,
            phase=MachineBootstrapPhase.Joining,
            failure_reason=None,
        )

    def report_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
        *,
        peer_address: str = "",
    ) -> ProviderNodeBootstrapFailureResponse:
        pool, connection = self._enrollment_target(request)
        self._authorize_launch(request, pool)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
            peer_address=peer_address,
            launch_id=request.launch_id,
        )
        excerpt = _sanitized_excerpt(request.diagnostic_excerpt)
        if request.provider is not ProviderKind.Aws:
            for credential in (request.bootstrap_token, request.node_agent_token):
                if credential:
                    excerpt = excerpt.replace(credential, "[redacted]")
            excerpt = re.sub(
                r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,128}(?![A-Za-z0-9_-])",
                "[redacted]",
                excerpt,
            )
        observed = self.compute.record_provider_bootstrap_status(
            pool_id=pool.id,
            provider_instance_id=request.provider_instance_id,
            phase=MachineBootstrapPhase.Failed,
            failure_reason=request.failure_reason,
            failure_detail=excerpt,
        )
        if self.events is not None:
            with suppress(Exception):
                self.events.emit(
                    "provider-node.bootstrap-failed",
                    resource_type="provider-instance",
                    resource_id=request.provider_instance_id,
                    message=(
                        f"bootstrap failed: {request.failure_reason.value} on pool {pool.name}"
                    ),
                    level=EventLevel.Error,
                    data={
                        "pool": pool.name,
                        "failure_reason": request.failure_reason.value,
                        "diagnostic_excerpt": excerpt,
                    },
                    workspace_id=pool.workspace_id,
                )
        return ProviderNodeBootstrapFailureResponse(
            provider_instance_id=request.provider_instance_id,
            phase=observed.bootstrap_phase,
            failure_reason=observed.bootstrap_failure_reason,
            observed_at=observed.bootstrap_observed_at,
        )

    def record_phase(
        self,
        request: ProviderNodeBootstrapPhaseRequest,
        *,
        peer_address: str = "",
    ) -> ProviderNodeBootstrapFailureResponse:
        pool, connection = self._enrollment_target(request)
        self._authorize_launch(request, pool)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
            peer_address=peer_address,
            launch_id=request.launch_id,
        )
        observed = self.compute.record_provider_bootstrap_status(
            pool_id=pool.id,
            provider_instance_id=request.provider_instance_id,
            phase=request.phase,
            failure_reason=None,
        )
        return ProviderNodeBootstrapFailureResponse(
            provider_instance_id=request.provider_instance_id,
            phase=observed.bootstrap_phase,
            failure_reason=observed.bootstrap_failure_reason,
            observed_at=observed.bootstrap_observed_at,
        )

    def _verify_active_node(
        self,
        *,
        pool: ComputeUnitRecord,
        connection: AwsAccountConnection | None,
        provider: ProviderKind,
        region: str,
        provider_instance_id: str,
        identity_proof_url: str,
        peer_address: str,
        launch_id: str = "",
    ) -> None:
        if not pool.provider_state.resource_id:
            raise UpstreamUnavailableError("provider pool identity is not established")
        known = self._known_instance_ids(pool)
        if provider_instance_id not in known:
            # An instance can report before the reconciler has observed it. Ask
            # for one refresh — rate limited per pool, because this route is
            # unauthenticated and the refresh calls the customer's AWS account.
            if not self._request_inventory_refresh(pool):
                raise UpstreamUnavailableError("provider inventory is still refreshing")
            known = self._known_instance_ids(pool)
        # Verification makes an outbound call from a synchronous route, so it
        # occupies a request-handling thread for as long as it runs. Without a
        # bound, enough unauthenticated callers stall every synchronous route
        # in the process — not merely enrolment.
        with self._proof_capacity():
            self.identity_verifier.verify(
                ProviderNodeIdentityProof(
                    launch_id=launch_id,
                    provider=provider,
                    region=region,
                    provider_instance_id=provider_instance_id,
                    proof_url=SecretStr(identity_proof_url),
                    peer_address=peer_address,
                ),
                pool=pool,
                connection=connection,
                provider_instance_ids=known,
            )

    def _require_launches(self) -> ProviderNodeLaunchService:
        if self.launches is None:
            raise UpstreamUnavailableError("provider launch authorization is not configured")
        return self.launches

    def _authorize_launch(
        self,
        request: (
            ProviderNodeEnrollmentRequest
            | ProviderNodeBootstrapFailureRequest
            | ProviderNodeBootstrapPhaseRequest
        ),
        pool: ComputeUnitRecord,
    ) -> None:
        if request.provider is not ProviderKind.Aws:
            self._require_launches().authorize(request, pool)

    @contextmanager
    def _proof_capacity(self) -> Iterator[None]:
        redis = self.rate_limiter
        if redis is None:
            yield
            return
        token = token_urlsafe(16)
        key = "provider-node:proof-inflight"
        if not try_acquire_slot(
            redis,
            key,
            token,
            limit=self.proof_max_inflight,
            ttl_seconds=_PROOF_SLOT_TTL_SECONDS,
            now_seconds=time.monotonic(),
        ):
            raise UpstreamUnavailableError("identity verification is at capacity")
        try:
            yield
        finally:
            with suppress(Exception):
                release_slot(redis, key, token)

    def _known_instance_ids(self, pool: ComputeUnitRecord) -> tuple[str, ...]:
        """Instances this pool owns, from durable inventory.

        Read, never a provider call: this runs before the caller has proved who
        it is, on a route anyone can reach.
        """
        with self.gateway.services.context.database.session() as session:
            records = ComputeProviderInstanceRepository(session).list_for_pool(pool.id)
        return tuple(record.instance_id for record in records if record.instance_id)

    def _request_inventory_refresh(self, pool: ComputeUnitRecord) -> bool:
        redis = self.rate_limiter
        if redis is None:
            return False
        if not try_consume(
            redis,
            f"provider-node:inventory-refresh:{pool.id}",
            limit=1,
            window_seconds=_INVENTORY_REFRESH_WINDOW_SECONDS,
        ):
            return False
        with suppress(Exception):
            self.compute.describe_internal_unit(pool.workspace_id, pool.capacity_owner_id)
        return True

    def _enrollment_target(
        self,
        request: (
            ProviderNodeEnrollmentRequest
            | ProviderNodeBootstrapFailureRequest
            | ProviderNodeBootstrapPhaseRequest
        ),
    ) -> tuple[ComputeUnitRecord, AwsAccountConnection | None]:
        with self.gateway.services.context.database.session() as session:
            return self._enrollment_target_in_transaction(session, request, for_update=False)

    def _enrollment_target_in_transaction(
        self,
        session: DatabaseSession,
        request: (
            ProviderNodeEnrollmentRequest
            | ProviderNodeBootstrapFailureRequest
            | ProviderNodeBootstrapPhaseRequest
        ),
        *,
        for_update: bool = True,
    ) -> tuple[ComputeUnitRecord, AwsAccountConnection | None]:
        units = ComputeUnitRepository(session)
        pool = units.get(request.enrollment_request_id)
        if pool is not None and for_update:
            WorkspaceRepository(session).lock_active_owner(pool.workspace_id)
            pool = units.get(request.enrollment_request_id, for_update=True)
        if pool is None:
            raise InvalidInputError("provider node enrollment request was not found")
        if (
            pool.visibility is not ComputeUnitVisibility.Internal
            or pool.capacity_mode is not ComputeCapacityMode.Pooled
            or pool.phase not in _ENROLLABLE_UNIT_PHASES
            or not pool.provider_ref.startswith(f"{request.provider.value}:")
            or pool.region != request.region
        ):
            raise InvalidInputError("provider node enrollment request is not active")
        if request.provider is not ProviderKind.Aws:
            if not pool.platform_fleet or pool.provider_connection_id is not None:
                raise InvalidInputError("bootstrap provider binding is not platform capacity")
            return pool, None
        if pool.provider_connection_id is None:
            raise InvalidInputError("AWS provider connection is unavailable")
        connection = AwsAccountConnectionRepository(session).get(pool.provider_connection_id)
        owner = WorkspaceMemberRepository(session).owner(pool.workspace_id)
        if (
            connection is None
            or connection.id != pool.provider_ref.removeprefix("aws:")
            or owner is None
            or connection.user_id != owner.user_id
            or not connection.hosts_workloads
            or connection.active_authorization is None
            or connection.active_authorization.phase is not AwsAccountAuthorizationPhase.Ready
        ):
            raise InvalidInputError("provider node connection is not active")
        return pool, connection

    def _pool_owner(self, session: DatabaseSession, pool: ComputeUnitRecord) -> str:
        owner = WorkspaceMemberRepository(session).owner(pool.workspace_id)
        if owner is None:
            raise InvalidInputError("provider capacity workspace has no owner")
        return owner.user_id

    def _issue_join_token(
        self,
        session: DatabaseSession,
        unit_id: str,
        workspace_id: str,
        pool: MachinePool,
        capacity_owner_id: str,
        *,
        owner_user_id: str,
    ) -> SecretStr:
        # The account is the connection's, which `_enrollment_target` has already
        # proved is the owner of this unit's workspace. An instance the customer's
        # own account launched belongs to that customer, exactly as a joined host does.
        plan = plan_join_token_creation(
            ComputePrincipal(
                workspace_id=workspace_id,
                owner_token_id=unit_id,
            ),
            pool,
            capacity_owner_id=capacity_owner_id,
            owner_user_id=owner_user_id,
            ttl="2m",
            max_uses=1,
        )
        ComputeJoinCredentialRepository(session).create(
            token_hash=plan.token_hash,
            user_id=owner_user_id,
            workspace_id=workspace_id,
            capacity_owner_id=capacity_owner_id,
            pool=pool,
            created_by_token_id=None,
            max_uses=1,
            expires_at=plan.expires_at,
        )
        return SecretStr(plan.token)


__all__ = ["ProviderNodeEnrollmentService"]

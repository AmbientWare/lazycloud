from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from secrets import token_urlsafe

from compute.agent_control import ComputePrincipal, plan_join_token_creation
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
from database.repositories.identity import WorkspaceMemberRepository
from provider_aws.provider_node_identity import AWS_STS_PROOF_TIMEOUT_SECONDS
from pydantic import SecretStr
from redis.exceptions import RedisError
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
from shared.timestamps import utc_now

from gateway.events import GatewayEventSink
from gateway.http import JoinAgentRequest, JoinAgentResponse, LeaveAgentRequest
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
    events: GatewayEventSink | None = None
    rate_limiter: RedisClient | None = None
    proof_max_inflight: int = 8

    def enroll(self, request: ProviderNodeEnrollmentRequest) -> JoinAgentResponse:
        pool, connection = self._enrollment_target(request)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
        )
        join_token = self._issue_join_token(
            pool.id,
            pool.workspace_id,
            pool.pool,
            pool.capacity_owner_id,
            owner_user_id=connection.user_id,
        )
        joined = self.gateway.join_agent(
            JoinAgentRequest(
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
        )
        try:
            if joined.workspace_id != pool.workspace_id or joined.pool != pool.pool:
                raise ConflictError("provider node joined a different compute pool")
            with self.gateway.services.context.database.session() as session:
                bound = ComputeProviderInstanceRepository(session).bind_machine(
                    pool.id,
                    request.provider_instance_id,
                    joined.machine_id,
                )
            if bound is None:
                raise ConflictError("provider node is no longer available for enrollment")
            self.compute.record_provider_bootstrap_status(
                pool_id=pool.id,
                provider_instance_id=request.provider_instance_id,
                phase=MachineBootstrapPhase.Joining,
                failure_reason=None,
            )
        except Exception as exc:
            # Leaving the agent deletes the machine, so the binding written above
            # has to go with it. A reference to a deleted machine fails the
            # foreign key on every later pool sync, which takes down enrollment
            # for the whole pool — including the report that would explain this
            # failure.
            with suppress(Exception):
                self._release_machine_binding(pool.id, request.provider_instance_id, joined)
            with suppress(Exception):
                self.gateway.leave_agent(LeaveAgentRequest(agent_token=joined.agent_token))
            if self.events is not None:
                # Recording the rollback must never replace the failure that
                # caused it.
                with suppress(Exception):
                    self.events.emit(
                        "provider-node.enrollment-rolled-back",
                        resource_type="provider-instance",
                        resource_id=request.provider_instance_id,
                        message=(
                            f"enrollment failed on pool {pool.name}; machine "
                            f"binding and agent were rolled back "
                            f"({type(exc).__name__})"
                        ),
                        level=EventLevel.Error,
                        data={
                            "pool": pool.name,
                            "operation": "enroll",
                            "error_type": type(exc).__name__,
                        },
                        workspace_id=pool.workspace_id,
                    )
            raise
        return joined

    def _release_machine_binding(
        self,
        pool_id: str,
        provider_instance_id: str,
        joined: JoinAgentResponse,
    ) -> None:
        with self.gateway.services.context.database.session() as session:
            ComputeProviderInstanceRepository(session).unbind_machine(
                pool_id,
                provider_instance_id,
                joined.machine_id,
            )

    def report_failure(
        self,
        request: ProviderNodeBootstrapFailureRequest,
    ) -> ProviderNodeBootstrapFailureResponse:
        pool, connection = self._enrollment_target(request)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
        )
        excerpt = _sanitized_excerpt(request.diagnostic_excerpt)
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
    ) -> ProviderNodeBootstrapFailureResponse:
        pool, connection = self._enrollment_target(request)
        self._verify_active_node(
            pool=pool,
            connection=connection,
            provider=request.provider,
            region=request.region,
            provider_instance_id=request.provider_instance_id,
            identity_proof_url=request.identity_proof_url,
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
        connection: AwsAccountConnection,
        provider: ProviderKind,
        region: str,
        provider_instance_id: str,
        identity_proof_url: str,
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
                    provider=provider,
                    region=region,
                    provider_instance_id=provider_instance_id,
                    proof_url=SecretStr(identity_proof_url),
                ),
                pool=pool,
                connection=connection,
                provider_instance_ids=known,
            )

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
            self.compute.describe_internal_unit(pool.workspace_id, pool.name)
        return True

    def _enrollment_target(
        self,
        request: (
            ProviderNodeEnrollmentRequest
            | ProviderNodeBootstrapFailureRequest
            | ProviderNodeBootstrapPhaseRequest
        ),
    ) -> tuple[ComputeUnitRecord, AwsAccountConnection]:
        if request.provider is not ProviderKind.Aws:
            raise InvalidInputError(f"unsupported provider node: {request.provider.value}")
        with self.gateway.services.context.database.session() as session:
            pool = ComputeUnitRepository(session).get(request.enrollment_request_id)
            if pool is None:
                raise InvalidInputError("provider node enrollment request was not found")
            if (
                pool.visibility is not ComputeUnitVisibility.Internal
                or pool.capacity_mode is not ComputeCapacityMode.Pooled
                or pool.phase not in _ENROLLABLE_UNIT_PHASES
                or not pool.provider_ref.startswith("aws:")
                or pool.provider_connection_id is None
                or pool.region != request.region
            ):
                raise InvalidInputError("provider node enrollment request is not active")
            connection = AwsAccountConnectionRepository(session).get(pool.provider_connection_id)
            # The account behind the unit's workspace, not the workspace itself: one
            # connection backs every workspace its owner holds, so the tenancy check
            # is that the unit and the connection answer to the same owner.
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

    def _issue_join_token(
        self,
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
        with self.gateway.services.context.database.session() as session:
            unit = ComputeUnitRepository(session).get(unit_id, for_update=True)
            if (
                unit is None
                or unit.workspace_id != workspace_id
                or unit.capacity_owner_id != capacity_owner_id
                or unit.pool != pool
                or unit.phase not in _ENROLLABLE_UNIT_PHASES
            ):
                raise ConflictError("provider node enrollment request changed")
            credentials = ComputeJoinCredentialRepository(session)
            durable = credentials.create(
                token_hash=plan.token_hash,
                user_id=owner_user_id,
                workspace_id=workspace_id,
                capacity_owner_id=unit.capacity_owner_id,
                pool=unit.pool,
                created_by_token_id=None,
                max_uses=1,
                expires_at=plan.expires_at,
            )
        state = plan.state.model_copy(
            update={
                "credential_id": durable.id,
                "capacity_owner_id": unit.capacity_owner_id,
                "created_by_token_id": "provider-node",
            }
        )
        try:
            self.gateway.compute_states.save_join_token_state(
                state,
                ttl_seconds=plan.ttl_seconds,
            )
        except RedisError as exc:
            with self.gateway.services.context.database.session() as session:
                credentials = ComputeJoinCredentialRepository(session)
                current = credentials.get(durable.id, for_update=True)
                if current is not None:
                    credentials.save(current.revoke(now=utc_now()))
            raise UpstreamUnavailableError(
                "provider node enrollment coordination is unavailable"
            ) from exc
        return SecretStr(plan.token)


__all__ = ["ProviderNodeEnrollmentService"]

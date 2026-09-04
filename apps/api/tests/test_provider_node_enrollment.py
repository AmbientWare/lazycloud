from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import count
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from compute.offers import ComputeOffer
from compute.providers import (
    ComputeProviderResolver,
    ProviderCapacityPhase,
    ProviderUnitBootstrap,
    ProviderUnitInstance,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from compute.service import ComputeService
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from provider_aws import AWS_STS_PROOF_NONCE_KEY
from provider_clients import AwsProviderNodeIdentityAdapter, ProviderNodeIdentityHttpResponse
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_enrollment import (
    MachineBootstrapFailureReason,
    MachineBootstrapPhase,
)
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeCapacity,
    ProviderNodeEnrollmentRequest,
)
from shared.provider_config import ProviderKind
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

_ACCOUNT_ID = "123456789012"
_CONNECTION_ID = "11111111-1111-4111-8111-111111111111"
_INSTANCE_ID = "i-0123456789abcdef0"
_REGION = "us-east-1"
_ROLE_NAME = "compute-node"
_NODE_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/{_ROLE_NAME}"
_NODE_PROFILE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/{_ROLE_NAME}"
_ASG_NAME = "workspace-compute-pool"
_OFFER_ID = f"{_REGION}:m7i.xlarge"


@dataclass(slots=True)
class _IdentityHttpResponse:
    status_code: int = 200
    body: bytes = b""
    content_type: str = "application/xml"
    content_length: int | None = None


@dataclass(frozen=True, slots=True)
class _IdentityHttpClient:
    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> ProviderNodeIdentityHttpResponse:
        del url, timeout_seconds, max_response_bytes, follow_redirects
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
            "<GetCallerIdentityResult>"
            f"<Arn>arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{_ROLE_NAME}/{_INSTANCE_ID}</Arn>"
            f"<UserId>AROA0123456789ABCDEFG:{_INSTANCE_ID}</UserId>"
            f"<Account>{_ACCOUNT_ID}</Account>"
            "</GetCallerIdentityResult>"
            "<ResponseMetadata><RequestId>01234567-89ab-cdef-0123-456789abcdef</RequestId>"
            "</ResponseMetadata></GetCallerIdentityResponse>"
        ).encode()
        return _IdentityHttpResponse(body=body, content_length=len(body))


@dataclass(slots=True)
class _ReplayGuard:
    claims: int = 0

    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool:
        del proof_sha256, expires_at
        self.claims += 1
        return self.claims == 1


@dataclass(frozen=True, slots=True)
class _PooledProvider:
    resource_id: str = _ASG_NAME

    def list_offers(self) -> Iterable[ComputeOffer]:
        return (_offer(),)

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.describe_unit(request)

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready,
            resource_id=self.resource_id,
            desired_machines=1,
            max_machines=request.max_machines,
            observed_machines=1,
            instances=[
                ProviderUnitInstance(
                    provider_instance_id=_INSTANCE_ID,
                    status="active",
                    storage_volume_ids=("vol-00000000000000001",),
                )
            ],
            provider_state=ComputeUnitProviderState(resource_id=self.resource_id),
        )

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        del desired_machines, max_machines
        return self.describe_unit(request)

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        del provider_instance_id
        return self.describe_unit(request)

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.describe_unit(request).model_copy(
            update={"phase": ProviderCapacityPhase.Deleted}
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        del request, provider_instance_id
        return bool(storage_volume_ids)


@dataclass(frozen=True, slots=True)
class _Resolver(ComputeProviderResolver):
    provider: _PooledProvider
    policy: ResolvedProviderPolicy

    def list_platform_providers(self) -> Iterable[ResolvedComputeProvider]:
        return ()

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        del workspace_id
        return (self._resolved(),)

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        del workspace_id
        if provider_ref != f"aws:{_CONNECTION_ID}":
            raise KeyError(provider_ref)
        return self._resolved()

    def _resolved(self) -> ResolvedComputeProvider:
        return ResolvedComputeProvider(
            ref=f"aws:{_CONNECTION_ID}",
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=_CONNECTION_ID,
            pooled=self.provider,
            policy=self.policy,
        )


def _workspace_owner_id(services: ApiServices) -> str:
    """The account that owns the default workspace; connections hang off it."""
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    return user_id


def test_provider_node_enrollment_rejects_cross_workspace_connection(
    isolated_services: ApiServices,
) -> None:
    default_pool = _seed_connection_and_pool(isolated_services)
    other_workspace = owned_workspace(ControlPlaneService(isolated_services.context), "other")
    cross_workspace_pool = _pool(
        workspace_id=other_workspace.id,
        pool_id=str(uuid4()),
        name="cross-workspace",
    )
    with isolated_services.context.database.session() as session:
        ComputeUnitRepository(session).upsert(cross_workspace_pool)
    enrollment = _service(isolated_services, _PooledProvider())

    with pytest.raises(InvalidInputError, match="connection is not active"):
        enrollment.enroll(_request(cross_workspace_pool.id))

    assert default_pool.workspace_id != cross_workspace_pool.workspace_id


def test_provider_node_enrollment_rejects_an_instance_the_pool_does_not_own(
    isolated_services: ApiServices,
) -> None:
    """Membership is decided from durable inventory, not from a provider call.

    This route is unauthenticated, so it must not reach the customer's AWS
    account to answer. An instance the reconciler has not recorded for this
    pool is refused, and the one refresh it may request is rate limited.
    """
    pool = _seed_connection_and_pool(isolated_services)
    enrollment = _service(isolated_services, _PooledProvider())

    with pytest.raises(UpstreamUnavailableError, match="still refreshing"):
        enrollment.enroll(_request(pool.id, provider_instance_id="i-0fedcba987654321f"))


def test_provider_enrollment_resume_preserves_existing_agent_authority(
    isolated_services: ApiServices,
) -> None:
    pool = _seed_connection_and_pool(isolated_services)
    service = _service(isolated_services, _PooledProvider())
    request = _request(pool.id)
    service.compute.record_provider_bootstrap_status(
        pool_id=pool.id,
        provider_instance_id=_INSTANCE_ID,
        phase=MachineBootstrapPhase.Booting,
        failure_reason=None,
    )
    joined = service.enroll(request)
    first = service.gateway.resume_provider_agent(
        node_agent_token=SecretStr(joined.agent_token),
        pool=pool,
        machine_fingerprint=request.machine_fingerprint,
    )
    second = service.gateway.resume_provider_agent(
        node_agent_token=SecretStr(joined.agent_token),
        pool=pool,
        machine_fingerprint=request.machine_fingerprint,
    )
    assert first is not None and second is not None
    assert first.machine_id == second.machine_id == joined.machine_id
    assert (
        first.credential_generation == second.credential_generation == joined.credential_generation
    )
    assert first.credential_id == second.credential_id == joined.credential_id
    with pytest.raises(InvalidInputError):
        service.gateway.resume_provider_agent(
            node_agent_token=SecretStr(joined.agent_token),
            pool=pool,
            machine_fingerprint="another-host",
        )


def test_provider_node_bootstrap_failure_is_durable_after_identity_verification(
    isolated_services: ApiServices,
) -> None:
    pool = _seed_connection_and_pool(isolated_services)
    enrollment = _service(isolated_services, _PooledProvider())

    observed = enrollment.report_failure(
        ProviderNodeBootstrapFailureRequest(
            enrollment_request_id=pool.id,
            provider=ProviderKind.Aws,
            region=_REGION,
            provider_instance_id=_INSTANCE_ID,
            identity_proof_url=_presigned_url(),
            failure_reason=MachineBootstrapFailureReason.AgentEnrollmentFailed,
        )
    )

    assert observed.phase is MachineBootstrapPhase.Failed
    assert observed.failure_reason is MachineBootstrapFailureReason.AgentEnrollmentFailed


def test_bootstrap_failure_excerpt_is_sanitized_persisted_and_leaves_an_event(
    isolated_services: ApiServices,
) -> None:
    """The excerpt is the only diagnosis that leaves an unreachable machine.

    It is accepted only behind identity verification, has its control
    characters stripped before it becomes durable, and produces an error event
    an operator can find without knowing which instance to ask about.
    """
    pool = _seed_connection_and_pool(isolated_services)
    enrollment = replace(
        _service(isolated_services, _PooledProvider()), events=isolated_services.events
    )

    enrollment.report_failure(
        ProviderNodeBootstrapFailureRequest(
            enrollment_request_id=pool.id,
            provider=ProviderKind.Aws,
            region=_REGION,
            provider_instance_id=_INSTANCE_ID,
            identity_proof_url=_presigned_url(),
            failure_reason=MachineBootstrapFailureReason.AgentDownloadFailed,
            diagnostic_excerpt="curl: (22) 404\x1b[31m for artifact\x00 url",
        )
    )

    with isolated_services.context.database.session() as session:
        record = ComputeProviderInstanceRepository(session).get_for_pool_instance(
            pool.id,
            _INSTANCE_ID,
        )
    assert record is not None
    assert record.bootstrap_failure_detail == "curl: (22) 404[31m for artifact url"
    events = [
        event
        for event in isolated_services.events.list()
        if event.action == "provider-node.bootstrap-failed"
    ]
    assert len(events) == 1
    assert events[0].level is EventLevel.Error
    assert events[0].data["failure_reason"] == "agent_download_failed"


def test_provider_node_bootstrap_phase_is_durable_after_identity_verification(
    isolated_services: ApiServices,
) -> None:
    pool = _seed_connection_and_pool(isolated_services)
    enrollment = _service(isolated_services, _PooledProvider())

    observed = enrollment.record_phase(
        ProviderNodeBootstrapPhaseRequest(
            enrollment_request_id=pool.id,
            provider=ProviderKind.Aws,
            region=_REGION,
            provider_instance_id=_INSTANCE_ID,
            identity_proof_url=_presigned_url(),
            phase=MachineBootstrapPhase.Booting,
        )
    )

    assert observed.phase is MachineBootstrapPhase.Booting
    assert observed.failure_reason is None


def _service(
    isolated_services: ApiServices, provider: _PooledProvider
) -> ProviderNodeEnrollmentService:
    return ProviderNodeEnrollmentService(
        gateway=replace(
            isolated_services.gateway_service,
            compute_state=RedisComputeStateRepository(isolated_services.redis()),
        ),
        compute=_compute(isolated_services, provider),
        identity_verifier=AwsProviderNodeIdentityAdapter(
            http_client=_IdentityHttpClient(),
            replay_guard=_ReplayGuard(),
        ),
    )


def _compute(isolated_services: ApiServices, provider: _PooledProvider) -> ComputeService:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        connection = AwsAccountConnectionRepository(session).get(_CONNECTION_ID)
    assert connection is not None
    return ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(
            provider,
            ResolvedProviderPolicy(
                workspace_id=workspace_id,
                pool=connection.pool,
                platform_fleet=connection.platform_fleet,
                default_region=connection.compute.default_region,
                allowed_regions=connection.compute.allowed_regions,
                max_cpu_instances=connection.compute.max_cpu_instances,
                max_gpu_instances=connection.compute.max_gpu_instances,
            ),
        ),
        pool_bootstrap_factory=_bootstrap,
    )


def _seed_connection_and_pool(
    isolated_services: ApiServices,
    *,
    reconnecting: bool = False,
) -> ComputeUnitRecord:
    now = datetime.now(UTC)
    owner_id = _workspace_owner_id(isolated_services)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        authorization = AwsAccountAuthorizationGeneration(
            id=str(uuid4()),
            generation=1,
            role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/compute-control",
            authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
            phase=AwsAccountAuthorizationPhase.Ready,
            last_validated_at=now,
            created_at=now,
            updated_at=now,
        )
        pending = (
            AwsAccountAuthorizationGeneration(
                id=str(uuid4()),
                generation=2,
                role_arn=f"arn:aws:iam::{_ACCOUNT_ID}:role/compute-control",
                authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
                phase=AwsAccountAuthorizationPhase.AwaitingAuthorization,
                created_at=now,
                updated_at=now,
            )
            if reconnecting
            else None
        )
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=_CONNECTION_ID,
                user_id=owner_id,
                account_id=_ACCOUNT_ID,
                external_id="x" * 48,
                phase=(
                    AwsAccountConnectionPhase.ReconnectPending
                    if reconnecting
                    else AwsAccountConnectionPhase.Ready
                ),
                active_authorization=authorization,
                pending_authorization=pending,
                node_role_arn=_NODE_ROLE_ARN,
                node_instance_profile_arn=_NODE_PROFILE_ARN,
                created_at=now,
                updated_at=now,
            )
        )
        pool = ComputeUnitRepository(session).upsert(
            _pool(
                workspace_id=workspace_id,
                pool_id=str(uuid4()),
                name="aws-capacity",
            )
        )
        # The verifier reads the reconciler's durable inventory, never the
        # provider, so a node this pool owns has to be in it.
        ComputeProviderInstanceRepository(session).records.create(
            {
                "id": str(uuid4()),
                "provider": pool.provider_ref,
                "offer_id": _OFFER_ID,
                "status": "active",
                "source": "workspace_policy",
                "pool_id": pool.id,
                "instance_type": "m7i.xlarge",
                "instance_id": _INSTANCE_ID,
            },
            status="active",
        )
        return pool


def _pool(*, workspace_id: str, pool_id: str, name: str) -> ComputeUnitRecord:
    return ComputeUnitRecord(
        id=pool_id,
        capacity_owner_id=pool_id,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        workspace_id=workspace_id,
        name=UnitName(name),
        pool=MachinePool(name),
        selector=name,
        status=ComputeUnitPhase.Ready.value,
        source="workspace_policy",
        config={"root_volume_gib": 200},
        provider_ref=f"aws:{_CONNECTION_ID}",
        provider_connection_id=_CONNECTION_ID,
        capacity_mode=ComputeCapacityMode.Pooled,
        visibility=ComputeUnitVisibility.Internal,
        region=_REGION,
        offer_id=_OFFER_ID,
        capability_key="aws:us-east-1:m7i.xlarge:amd64:runsc",
        desired_machines=1,
        min_machines=0,
        max_machines=10,
        observed_machines=1,
        phase=ComputeUnitPhase.Ready,
        provider_state=ComputeUnitProviderState(resource_id=_ASG_NAME),
    )


def _request(
    pool_id: str,
    *,
    provider_instance_id: str = _INSTANCE_ID,
) -> ProviderNodeEnrollmentRequest:
    return ProviderNodeEnrollmentRequest(
        enrollment_request_id=pool_id,
        provider=ProviderKind.Aws,
        region=_REGION,
        provider_instance_id=provider_instance_id,
        identity_proof_url=_presigned_url(),
        machine_fingerprint="provider-node-machine",
        hostname="ip-10-0-0-10",
        os="linux",
        arch="amd64",
        executor="runsc",
        capacity=ProviderNodeCapacity(
            cpu_count=4,
            cpu_millicores=4_000,
            memory_mb=32 * 1024,
        ),
    )


_PROOF_NONCE = count(1)


def _presigned_url() -> str:
    request = AWSRequest(
        method="GET",
        url=(
            f"https://sts.{_REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
            f"&{AWS_STS_PROOF_NONCE_KEY}={next(_PROOF_NONCE):032x}"
        ),
    )
    SigV4QueryAuth(
        Credentials(
            access_key="ASIA0123456789ABCDEF",
            secret_key="0123456789abcdefghijklmnopqrstuvwxyzABCD",
            token="temporary/session-token",
        ),
        "sts",
        _REGION,
        expires=30,
    ).add_auth(request)
    if request.url is None:
        raise AssertionError("SigV4 signer did not produce a URL")
    query = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))
    assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    return request.url


def _offer() -> ComputeOffer:
    return ComputeOffer(
        id=_OFFER_ID,
        provider=f"aws:{_CONNECTION_ID}",
        cloud="aws",
        instance_type="m7i.xlarge",
        region=_REGION,
        cpu_millicores=4_000,
        memory_mb=32 * 1024,
        storage_mb=200 * 1024,
        hourly_cost_micros=340_000,
        available=10,
        capacity_mode=ComputeCapacityMode.Pooled,
        capability_key="aws:us-east-1:m7i.xlarge:amd64:runsc",
        supports_scale_to_zero=True,
    )


class _Bootstrap:
    """A pool bootstrap provisioner that returns the configured public origin."""

    def __init__(self) -> None:
        self.released: list[str] = []

    def bootstrap(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitBootstrap:
        del offer
        return ProviderUnitBootstrap(
            control_plane_url="https://control.example.com",
            enrollment_request_id=pool.id,
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
        )

    def release(self, pool: ComputeUnitRecord) -> None:
        self.released.append(pool.id)


_bootstrap = _Bootstrap()


def test_degraded_pool_still_accepts_provider_node_enrollment(
    isolated_services: ApiServices,
) -> None:
    """A degraded pool must not refuse a healthy machine.

    Refusal is self-reinforcing: it is recorded as another bootstrap failure, so
    the pool can never recover on its own and the reports that would explain the
    original failure are discarded.
    """
    pool = _seed_connection_and_pool(isolated_services)
    with isolated_services.context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            pool.model_copy(update={"phase": ComputeUnitPhase.Degraded})
        )
    enrollment = _service(isolated_services, _PooledProvider())

    observed = enrollment.report_failure(
        ProviderNodeBootstrapFailureRequest(
            enrollment_request_id=pool.id,
            provider=ProviderKind.Aws,
            region=_REGION,
            provider_instance_id=_INSTANCE_ID,
            identity_proof_url=_presigned_url(),
            failure_reason=MachineBootstrapFailureReason.AgentEnrollmentFailed,
        )
    )

    assert observed.phase is MachineBootstrapPhase.Failed

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from itertools import count
from pathlib import Path
from secrets import token_urlsafe
from threading import Barrier
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx
import pytest
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from compute.agent_control import hash_compute_token
from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION
from compute.offers import ComputeOffer
from compute.providers import (
    ComputeProviderResolver,
    ProviderCapacityPhase,
    ProviderPurchaseLimit,
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
from coordination.redis_client import RedisSettings
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.tables.compute import ComputeJoinCredentialTable, ComputeMachineEnrollmentTable
from database.tables.orchestration import MachineTable, WorkerTable
from database.tables.provider_launches import ProviderNodeLaunchTable
from gateway.http import JoinAgentResponse
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from provider_aws import AWS_STS_PROOF_NONCE_KEY
from provider_clients import AwsProviderNodeIdentityAdapter, ProviderNodeIdentityHttpResponse
from provider_clients.provider_nodes import ProviderNodeIdentityRegistry
from provider_hetzner.client import HetznerClient
from provider_hetzner.identity import provider_label, verify_node
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
from shared.supplier_costs import SupplierCostTerms
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import ProgrammingError
from tests.backing_services import postgres_url
from tests.real_redis import RealRedisActors
from tests.service_fixtures import owned_workspace, service_graph, workspace_owner_user_id

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database

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

    def unit_offer(self, unit: ComputeUnitRecord) -> ComputeOffer:
        return _offer()

    def list_offers(self, *, root_volume_gib: int) -> Iterable[ComputeOffer]:
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


def test_provider_enrollment_is_atomic_across_single_connection_replicas(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = postgres_url()
    database_name = f"lazycloud_enrollment_{uuid4().hex}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin = create_engine(
        base_url, connect_args={"application_name": DatabaseApplicationName.Test.value}
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"CREATE DATABASE {identifier}"))
        bootstrap_database(database_url)
        settings = DatabaseSettings(
            url=database_url,
            application_name=DatabaseApplicationName.Test,
            pool_size=1,
            max_overflow=0,
            pool_timeout_seconds=1,
        )
        database = DatabaseClient.from_settings(settings)
        async_io = ApiAsyncIo.from_settings(
            settings,
            RedisSettings(url=real_redis_actors.url, key_prefix=real_redis_actors.prefix),
        )
        try:
            with service_graph(
                database,
                tmp_path,
                redis_client=real_redis_actors.client(),
                binary_redis_client=real_redis_actors.client(decode_responses=False),
                async_io=async_io,
            ) as services:
                with database.session() as session:
                    unit_id = str(uuid4())
                    pool = ComputeUnitRepository(session).upsert(
                        ComputeUnitRecord(
                            id=unit_id,
                            workspace_id=services.context.default_workspace_id(session),
                            name=UnitName("atomic-enrollment"),
                            pool=MachinePool("lazycloud"),
                            provider="hetzner",
                            provider_ref="hetzner:test",
                            platform_fleet=True,
                            capacity_mode=ComputeCapacityMode.Pooled,
                            visibility=ComputeUnitVisibility.Internal,
                            capacity_owner_id=unit_id,
                            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                            capacity_owner_source=CapacityOwnerSource.Provider,
                            region="ash",
                            offer_id="ash:ccx13",
                            capability_key="hetzner:ash:ccx13:amd64:runsc",
                            phase=ComputeUnitPhase.Ready,
                            status="ready",
                            provider_state=ComputeUnitProviderState(resource_id=unit_id),
                            desired_machines=1,
                            max_machines=1,
                        )
                    )
                    ComputeProviderInstanceRepository(session).records.create(
                        {
                            "id": str(uuid4()),
                            "provider": pool.provider_ref,
                            "offer_id": pool.offer_id,
                            "status": "active",
                            "source": "platform_policy",
                            "pool_id": pool.id,
                            "instance_type": "ccx13",
                            "instance_id": "123",
                            "bootstrap_phase": MachineBootstrapPhase.Booting,
                        },
                        status="active",
                    )
                offer = ComputeOffer(
                    id=pool.offer_id,
                    provider=pool.provider_ref,
                    instance_type="ccx13",
                    region=pool.region,
                    capacity_mode=ComputeCapacityMode.Pooled,
                )
                launch = services.provider_node_launches.prepare(
                    ProviderUnitRequest(
                        workspace_id=pool.workspace_id,
                        unit_id=pool.id,
                        unit_name=pool.name,
                        provider_ref=pool.provider_ref,
                        provider_connection_id=None,
                        generation=pool.generation,
                        desired_machines=1,
                        max_machines=1,
                        offer=offer,
                        bootstrap=_bootstrap.bootstrap(pool, offer),
                    ),
                    "node-slot",
                )
                services.provider_node_launches.bind(
                    launch.launch_id,
                    provider_ref=pool.provider_ref,
                    provider_instance_id="123",
                    unit_id=pool.id,
                    server_name="node-slot",
                    region=pool.region,
                    generation=pool.generation,
                )

                def provider_response(
                    transport: httpx.HTTPTransport, request: httpx.Request
                ) -> httpx.Response:
                    del transport
                    assert request.method == "GET"
                    assert str(request.url) == "https://api.hetzner.cloud/v1/servers/123"
                    return httpx.Response(
                        200,
                        json={
                            "server": {
                                "id": 123,
                                "name": "node-slot",
                                "status": "running",
                                "created": datetime.now(UTC).isoformat(),
                                "labels": {
                                    "lazycloud-managed": "true",
                                    "lazycloud-unit": pool.id,
                                    "lazycloud-provider": provider_label(pool.provider_ref),
                                    "lazycloud-launch": launch.launch_id,
                                },
                                "location": {
                                    "name": "ash",
                                    "description": "Ashburn",
                                    "network_zone": "us-east",
                                },
                                "server_type": {
                                    "id": 1,
                                    "name": "ccx13",
                                    "cores": 2,
                                    "memory": 8,
                                    "disk": 80,
                                    "architecture": "x86",
                                    "cpu_type": "dedicated",
                                    "prices": [],
                                },
                                "public_net": {},
                                "volumes": [],
                            }
                        },
                    )

                monkeypatch.setattr(httpx.HTTPTransport, "handle_request", provider_response)
                enrollment = ProviderNodeEnrollmentService(
                    gateway=services.gateway_service,
                    compute=ComputeService(services.context),
                    identity_verifier=ProviderNodeIdentityRegistry(
                        aws=AwsProviderNodeIdentityAdapter(
                            http_client=_IdentityHttpClient(), replay_guard=_ReplayGuard()
                        ),
                        bootstrap_nodes={
                            pool.provider_ref: partial(
                                verify_node, HetznerClient(SecretStr("test-token"))
                            )
                        },
                    ),
                    launches=services.provider_node_launches,
                    rate_limiter=services.redis(),
                )
                request = ProviderNodeEnrollmentRequest(
                    enrollment_request_id=pool.id,
                    provider=ProviderKind.Hetzner,
                    region=pool.region,
                    provider_instance_id="123",
                    identity_proof_url="provider-bootstrap",
                    launch_id=launch.launch_id,
                    bootstrap_token=launch.bootstrap_token.get_secret_value(),
                    node_agent_token=token_urlsafe(32),
                    machine_fingerprint="provider-node-machine",
                    hostname="node-slot",
                    os="linux",
                    arch="amd64",
                    executor="runsc",
                    capacity=ProviderNodeCapacity(
                        cpu_count=2, cpu_millicores=2_000, memory_mb=8 * 1024
                    ),
                )
                with database.session() as session:
                    session.execute(
                        text("""
                        CREATE FUNCTION reject_machine_binding() RETURNS trigger
                        LANGUAGE plpgsql AS $$ BEGIN
                            RAISE EXCEPTION 'provider binding rejected';
                        END $$
                    """)
                    )
                    session.execute(
                        text("""
                        CREATE TRIGGER reject_machine_binding
                        BEFORE UPDATE ON compute_provider_instances
                        FOR EACH ROW WHEN (NEW.machine_id IS NOT NULL)
                        EXECUTE FUNCTION reject_machine_binding()
                    """)
                    )
                with pytest.raises(ProgrammingError, match="provider binding rejected"):
                    enrollment.enroll(request)
                with database.session() as session:
                    for table in (
                        MachineTable,
                        WorkerTable,
                        ComputeMachineEnrollmentTable,
                        ComputeJoinCredentialTable,
                    ):
                        assert session.scalar(select(func.count()).select_from(table)) == 0
                    row = session.get(ProviderNodeLaunchTable, launch.launch_id)
                    assert row is not None and row.enrolled_at is None
                    assert row.fingerprint_hash is None
                    instance = ComputeProviderInstanceRepository(session).get_for_pool_instance(
                        pool.id, "123"
                    )
                    assert instance is not None and instance.machine_id is None
                    assert instance.bootstrap_phase is MachineBootstrapPhase.Booting
                    session.execute(
                        text("DROP TRIGGER reject_machine_binding ON compute_provider_instances")
                    )
                    session.execute(text("DROP FUNCTION reject_machine_binding()"))
                assert (
                    enrollment.gateway.compute_states.get_agent_token_state(
                        hash_compute_token(request.node_agent_token)
                    )
                    is None
                )

                retry = request.model_copy(update={"bootstrap_token": ""})
                barrier = Barrier(2)

                def enroll_concurrently(
                    service: ProviderNodeEnrollmentService,
                ) -> JoinAgentResponse | UpstreamUnavailableError:
                    barrier.wait()
                    try:
                        return service.enroll(retry)
                    except UpstreamUnavailableError as exc:
                        return exc

                replica_database = DatabaseClient.from_settings(settings)
                replica_services = ApiServices.create(
                    replica_database,
                    root=tmp_path / "replica",
                    create_schema=False,
                    redis_client=real_redis_actors.client(),
                    binary_redis_client=real_redis_actors.client(decode_responses=False),
                    agent_binary_settings=services.agent_binary_settings,
                    workspace_storage_issuer=services.workspace_storage_issuer,
                )
                try:
                    replica_enrollment = ProviderNodeEnrollmentService(
                        gateway=replica_services.gateway_service,
                        compute=replica_services.compute,
                        identity_verifier=enrollment.identity_verifier,
                        launches=replica_services.provider_node_launches,
                        rate_limiter=replica_services.redis(),
                    )
                    replicas = (enrollment, replica_enrollment)
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = tuple(executor.map(enroll_concurrently, replicas))
                    assert any(isinstance(result, JoinAgentResponse) for result in results)
                    first, second = (
                        service.enroll(retry)
                        if isinstance(result, UpstreamUnavailableError)
                        else result
                        for service, result in zip(replicas, results, strict=True)
                    )
                finally:
                    replica_services.close()
                assert first.machine_id == second.machine_id
                assert first.credential_id == second.credential_id
                assert first.credential_generation == second.credential_generation == 1
                with database.session() as session:
                    for table in (
                        MachineTable,
                        WorkerTable,
                        ComputeMachineEnrollmentTable,
                        ComputeJoinCredentialTable,
                    ):
                        assert session.scalar(select(func.count()).select_from(table)) == 1
                    credential = session.scalar(select(ComputeJoinCredentialTable))
                    assert credential is not None and credential.use_count == 1
                    row = session.get(ProviderNodeLaunchTable, launch.launch_id)
                    assert row is not None and row.enrolled_at is not None
                    instance = ComputeProviderInstanceRepository(session).get_for_pool_instance(
                        pool.id, "123"
                    )
                    assert instance is not None and instance.machine_id == first.machine_id
                    assert instance.bootstrap_phase is MachineBootstrapPhase.Joining
                state = enrollment.gateway.compute_states.get_agent_token_state(
                    hash_compute_token(request.node_agent_token)
                )
                assert state is not None and state.machine_id == first.machine_id
        finally:
            asyncio.run(async_io.close())
            database.engine.dispose()
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"))
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_database WHERE datname = :name"),
                    {"name": database_name},
                )
                == 0
            )
        admin.dispose()


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
                default_region=AWS_COMPUTE_CONFIGURATION.default_region,
                allowed_regions=AWS_COMPUTE_CONFIGURATION.allowed_regions,
                purchase_limits=(
                    ProviderPurchaseLimit(
                        region=_REGION,
                        instance_type="m7i.xlarge",
                        max_hourly_cost_micros=340_000,
                    ),
                ),
                max_cpu_instances=AWS_COMPUTE_CONFIGURATION.max_cpu_instances,
                max_gpu_instances=AWS_COMPUTE_CONFIGURATION.max_gpu_instances,
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
        cost_terms=SupplierCostTerms(
            compute_hourly_micros=340_000, root_disk_hourly_micros=0, public_ipv4_hourly_micros=0
        ),
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

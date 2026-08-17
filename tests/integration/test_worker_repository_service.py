from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.worker_repository_service import (
    WorkerRepositoryCacheStorage,
    WorkerRepositoryDependencies,
    WorkerRepositoryObjectStorage,
    WorkerRepositoryService,
)
from compute.agent_control import (
    TailnetConfig,
    agent_machine_worker_id,
)
from compute.state import (
    RedisComputeStateRepository,
)
from control.service import ControlPlaneService, StubKind
from coordination.event_bus import (
    EventBusEvent,
    EventBusEventType,
    RedisEventBus,
    event_id_for_event,
)
from coordination.redis_client import RedisClient
from database.context import ServiceContext
from database.repositories.compute import ComputeUnitRepository
from database.repositories.execution import TaskRepository
from database.repositories.images import (
    CheckpointRepository,
    ImageArchiveRepository,
    ImageRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
)
from execution.containers.preemption import PreemptedContainerService
from fastapi.testclient import TestClient
from foundation.network import worker_network_prefix
from gateway.http import (
    JoinAgentRequest,
    RegisterAgentTailnetDeviceRequest,
    RequestAgentTransportCredentialRequest,
    UpdateAgentRouteStatusRequest,
)
from gateway.service import GatewayControlService
from identity.auth import AuthorizationDeniedError, AuthService
from networking.tailnet_control import TailnetAuthKey, TailnetDevice
from operations.container_shutdown import ContainerShutdownService
from pydantic import JsonValue, SecretStr, TypeAdapter
from scheduler.containers import SchedulerContainerDispatchStatus
from scheduler.fleet import SchedulerContainerStatus, SchedulerWorkerStatus
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
    SchedulerContainerState,
    SchedulerWorkerRecord,
    SchedulerWorkerRequest,
)
from shared.app_identity import FUNCTION_IMAGE
from shared.cache_records import CacheEntry
from shared.compute_enrollment import ComputePreflightCheck, PreflightSeverity
from shared.compute_policy import (
    MachinePool,
    UnitName,
)
from shared.container_requests import ContainerShutdownTarget, StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.errors import ConflictError, UpstreamUnavailableError
from shared.http.errors import ErrorResponse
from shared.identity import AuthScope, TokenKind, WorkspaceStorageConfig
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import ImageRecord
from shared.objects import ObjectRecord
from shared.routing import AgentBackendRoute, BackendRouteState, BackendRouteTransport
from shared.source_cache_cleanup import (
    WorkerCacheGenerationRecord,
    WorkerCacheGenerationState,
)
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from storage.image_archive import ResolvedImageArchiveSettings
from storage_client.s3 import S3ObjectInfo, S3ObjectStoreSettings, S3PresignedUpload
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis
from tests.scheduler_composition import scheduler_request_service_for_redis
from tests.service_fixtures import owned_workspace, workspace_owner_user_id
from worker.checkpoints import (
    CheckpointStateOperation,
    CheckpointStatePayload,
    WorkerCheckpointStatus,
)
from worker.events import (
    ContainerLifecyclePayload,
)
from worker.image_lifecycle import ImageRegistryStore
from worker.origin_access import (
    CacheOriginCredentialRequest,
    ImageArchiveUploadCredentialRequest,
)
from worker.repository_payloads import (
    AcquireAutomaticCheckpointLeaseRequest,
    DeleteContainerStateRequest,
    GetCacheOriginCredentialsResponse,
    GetCheckpointRestoreRequest,
    GetContainerCredentialsResponse,
    GetImageBuildCredentialsRequest,
    GetNextContainerRequestRequest,
    PersistCheckpointArchiveRequest,
    PrepareCheckpointArchiveUploadRequest,
    PrepareImageBuildContextDownloadRequest,
    PublishContainerLifecycleRequest,
    RecordWorkerUsageResponse,
    ReleaseAutomaticCheckpointLeaseRequest,
    SaveCheckpointStateRequest,
    SetContainerAddressMapRequest,
    SetContainerAddressRequest,
    SetContainerExitCodeRequest,
    UpdateContainerStatusRequest,
    WorkerCacheSession,
    WorkerCacheSessionRequest,
    WorkerRecordResponse,
    WorkerRepositoryPrincipal,
)
from worker.tools import ContainerCredentialRequest
from worker_repository.credentials import WorkerCredentialService
from worker_repository.origin_credentials import (
    CacheOriginCredentialConfig,
    WorkerCacheOriginCredentialService,
)
from worker_repository.source_cache import (
    WorkerSourceCacheService,
    WorkerSourceCacheUnavailableError,
)

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


class _WorkerRepositoryTailnetControl:
    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey:
        return TailnetAuthKey(
            id=f"key-{machine_id}",
            key=SecretStr(f"secret-{hostname}"),
            expires_at=utc_now() + timedelta(minutes=5),
        )

    def revoke_auth_key(self, key_id: str) -> None:
        del key_id

    def verify_device(self, node_id: str, *, expected_hostname: str) -> TailnetDevice:
        return TailnetDevice(
            id=f"rest-{node_id}",
            node_id=node_id,
            hostname=expected_hostname,
            addresses=("100.64.0.10",),
            tags=("tag:lazycloud-agent",),
            authorized=True,
        )

    def find_devices(self, *, hostname: str) -> tuple[TailnetDevice, ...]:
        del hostname
        return ()

    def remove_device(self, device_id: str) -> None:
        del device_id


def test_image_build_credentials_reject_wrong_assigned_worker(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id="build-container-1",
            stub_id="image-build",
            workspace_id="workspace-1",
            worker_id="worker-1",
        )
    )
    request = GetImageBuildCredentialsRequest(
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="build-container-1",
        registry="registry.example.com",
        cache_key="credential-cache-key",
    )

    with pytest.raises(AuthorizationDeniedError, match="assigned worker"):
        service.get_image_build_credentials(
            request,
            principal=WorkerRepositoryPrincipal(
                workspace_id="workspace-1",
                worker_id="worker-2",
            ),
        )


def test_automatic_checkpoint_lease_is_bound_to_assigned_container_and_worker(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "default")
    workspace_owner_user_id(isolated_services.context, workspace.id)
    stub = control.create_stub("automatic-checkpoint-lease", workspace=workspace.id)
    container_id = str(uuid4())
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            stub_id=stub.id,
            workspace_id=workspace.id,
            worker_id="worker-1",
        )
    )
    request = AcquireAutomaticCheckpointLeaseRequest(
        workspace_id=workspace.id,
        stub_id=stub.id,
        container_id=container_id,
        ttl_seconds=2400,
    )
    assigned_worker = WorkerRepositoryPrincipal(
        workspace_id=workspace.id,
        worker_id="worker-1",
    )

    assert service.acquire_automatic_checkpoint_lease(
        request,
        principal=assigned_worker,
    ).acquired
    with pytest.raises(AuthorizationDeniedError, match="assigned worker"):
        service.release_automatic_checkpoint_lease(
            ReleaseAutomaticCheckpointLeaseRequest(
                workspace_id=workspace.id,
                stub_id=stub.id,
                container_id=container_id,
            ),
            principal=assigned_worker.model_copy(update={"worker_id": "worker-2"}),
        )
    assert service.release_automatic_checkpoint_lease(
        ReleaseAutomaticCheckpointLeaseRequest(
            workspace_id=workspace.id,
            stub_id=stub.id,
            container_id=container_id,
        ),
        principal=assigned_worker,
    ).released


def test_managed_image_build_credentials_use_assigned_workspace(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    workspace_id = "tenant-workspace"
    container_id = "build-container-tenant"
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            stub_id="image-build",
            workspace_id=workspace_id,
            worker_id="worker-1",
        )
    )
    request = GetImageBuildCredentialsRequest(
        workspace_id=workspace_id,
        build_id="build-tenant",
        container_id=container_id,
        registry="registry.example.com",
        cache_key="",
    )
    managed = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id="worker-1",
        token_kind=TokenKind.Worker,
    )
    # A private worker belonging to another account: tenancy is compared by account,
    # so the record has to exist for its owner to be read at all.
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            private_worker=True,
            owner_user_id="22222222-2222-4222-8222-222222222222",
            requires_pool_selector=True,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )

    with pytest.raises(
        AuthorizationDeniedError,
        match="workspace does not belong to the worker's account",
    ):
        service.get_image_build_credentials(
            request,
            principal=managed.model_copy(update={"token_kind": TokenKind.WorkerPrivate}),
        )
    with pytest.raises(AuthorizationDeniedError, match="requires a worker principal"):
        service.get_image_build_credentials(
            request,
            principal=managed.model_copy(update={"token_kind": TokenKind.Machine}),
        )

    response = service.get_image_build_credentials(request, principal=managed)

    assert response.private_inputs is None


def test_managed_container_credentials_use_assigned_workspace(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    workspace_id = "tenant-workspace"
    container_id = "tenant-container"
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            stub_id="tenant-stub",
            workspace_id=workspace_id,
            worker_id="worker-1",
        )
    )
    request = ContainerCredentialRequest(
        workspace_id=workspace_id,
        stub_id="tenant-stub",
        container_id=container_id,
    )
    managed = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id="worker-1",
        token_kind=TokenKind.Worker,
    )
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            private_worker=True,
            owner_user_id="22222222-2222-4222-8222-222222222222",
            requires_pool_selector=True,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )

    with pytest.raises(
        AuthorizationDeniedError,
        match="workspace does not belong to the worker's account",
    ):
        service.get_container_credentials(
            request,
            principal=managed.model_copy(update={"token_kind": TokenKind.WorkerPrivate}),
        )
    with pytest.raises(AuthorizationDeniedError, match="assigned worker"):
        service.get_container_credentials(
            request,
            principal=managed.model_copy(update={"worker_id": "worker-2"}),
        )
    with pytest.raises(AuthorizationDeniedError, match="requires a worker principal"):
        service.get_container_credentials(
            request,
            principal=managed.model_copy(update={"token_kind": TokenKind.Machine}),
        )

    response = service.get_container_credentials(request, principal=managed)

    assert response.credentials is not None
    assert response.credentials.env == []


def test_image_archive_upload_credentials_are_bound_and_one_time(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    archive_storage = _FakeObjectStorage()
    service = _worker_repository_service(
        isolated_services,
        redis,
        object_storage=archive_storage,
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    execution = isolated_services.images.start(ImageSpec(ignore_python=True, commands=["true"]))
    build = execution.record
    assert build.image_id
    capability = "a" * 32
    container_id = execution.session.container_id
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            stub_id="image-build",
            workspace_id=workspace_id,
            worker_id="worker-1",
            image_build_id=build.id,
            image_id=build.image_id,
            image_build_upload_capability=capability,
        )
    )
    service.origin_credentials.config = CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
    )
    service.origin_credentials.archive_settings = _archive_settings()
    service.origin_credentials.object_store_client = archive_storage
    isolated_services.images.archive_settings = _archive_settings()
    isolated_services.images.archive_store = archive_storage
    request = ImageArchiveUploadCredentialRequest(
        workspace_id=workspace_id,
        build_id=build.id,
        container_id=container_id,
        image_id=build.image_id,
        upload_capability=capability,
        archive_size_bytes=1024,
        archive_sha256="a" * 64,
    )
    principal = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id="worker-1",
        token_kind=TokenKind.Worker,
    )
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            private_worker=True,
            owner_user_id="22222222-2222-4222-8222-222222222222",
            requires_pool_selector=True,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )

    with pytest.raises(
        AuthorizationDeniedError,
        match="workspace does not belong to the worker's account",
    ):
        service.get_image_archive_upload_credentials(
            request,
            principal=principal.model_copy(update={"token_kind": TokenKind.WorkerPrivate}),
        )
    with pytest.raises(AuthorizationDeniedError, match="assigned worker"):
        service.get_image_archive_upload_credentials(
            request,
            principal=principal.model_copy(update={"worker_id": "worker-2"}),
        )
    with pytest.raises(AuthorizationDeniedError, match="assigned build"):
        service.get_image_archive_upload_credentials(
            request.model_copy(update={"image_id": "img_wrong"}),
            principal=principal,
        )

    with pytest.raises(AuthorizationDeniedError, match="capability does not match"):
        service.get_image_archive_upload_credentials(
            request.model_copy(update={"upload_capability": "b" * 32}),
            principal=principal,
        )

    response = service.get_image_archive_upload_credentials(request, principal=principal)
    assert response.credentials is not None
    assert response.credentials.ok
    assert response.credentials.object_key == f"image-archives/{build.image_id}.rclip"
    assert response.credentials.upload_url.startswith("memory://image-archives/image-archives/")
    with pytest.raises(AuthorizationDeniedError, match="already consumed"):
        service.get_image_archive_upload_credentials(request, principal=principal)

    stale_execution = isolated_services.images.start(
        ImageSpec(ignore_python=True, commands=["echo stale"])
    )
    stale_build = stale_execution.record
    assert stale_build.image_id
    stale_capability = "c" * 32
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=stale_execution.session.container_id,
            stub_id="image-build",
            workspace_id=workspace_id,
            worker_id="worker-1",
            image_build_id=stale_build.id,
            image_id=stale_build.image_id,
            image_build_upload_capability=stale_capability,
        )
    )
    isolated_services.images.fail(stale_build.id, "lease expired")
    with pytest.raises(AuthorizationDeniedError, match="ownership is no longer active"):
        service.get_image_archive_upload_credentials(
            ImageArchiveUploadCredentialRequest(
                workspace_id=workspace_id,
                build_id=stale_build.id,
                container_id=stale_execution.session.container_id,
                image_id=stale_build.image_id,
                upload_capability=stale_capability,
                archive_size_bytes=1024,
                archive_sha256="b" * 64,
            ),
            principal=principal,
        )


def test_image_build_context_download_is_bound_to_active_assignment_and_object(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    context = isolated_services.object_storage.put_bytes_for_workspace(
        workspace_id=workspace_id,
        bucket=isolated_services.object_storage.default_bucket,
        key="contexts/remote-v2.zip",
        data=b"remote-v2-build-context",
        content_type="application/zip",
    )
    execution = isolated_services.images.start(
        ImageSpec(
            ignore_python=True,
            context_object_id=context.id,
            context_digest=context.sha256,
        ),
        workspace_id=workspace_id,
    )
    build = execution.record
    assert build.image_id
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id=execution.session.container_id,
            stub_id="image-build",
            workspace_id=workspace_id,
            worker_id="worker-1",
            image_build_id=build.id,
            image_id=build.image_id,
        )
    )
    request = PrepareImageBuildContextDownloadRequest(
        workspace_id=workspace_id,
        build_id=build.id,
        container_id=execution.session.container_id,
        object_id=context.id,
    )
    principal = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id="worker-1",
        token_kind=TokenKind.Worker,
    )
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            private_worker=True,
            owner_user_id="22222222-2222-4222-8222-222222222222",
            requires_pool_selector=True,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )

    with pytest.raises(
        AuthorizationDeniedError,
        match="workspace does not belong to the worker's account",
    ):
        service.prepare_image_build_context_download(
            request,
            principal=principal.model_copy(update={"token_kind": TokenKind.WorkerPrivate}),
        )
    with pytest.raises(AuthorizationDeniedError, match="workspace does not match container"):
        service.prepare_image_build_context_download(
            request.model_copy(update={"workspace_id": "workspace-wrong"}),
            principal=principal,
        )
    with pytest.raises(AuthorizationDeniedError, match="assigned worker"):
        service.prepare_image_build_context_download(
            request,
            principal=principal.model_copy(update={"worker_id": "worker-2"}),
        )
    with pytest.raises(AuthorizationDeniedError, match="assigned build"):
        service.prepare_image_build_context_download(
            request.model_copy(update={"build_id": "build-wrong"}),
            principal=principal,
        )
    with pytest.raises(AuthorizationDeniedError, match="ownership is no longer active"):
        service.prepare_image_build_context_download(
            request.model_copy(update={"object_id": "object-wrong"}),
            principal=principal,
        )

    response = service.prepare_image_build_context_download(request, principal=principal)

    assert response.object_id == context.id
    assert response.content_length == context.size
    assert response.sha256 == context.sha256
    assert response.download_url
    assert response.expires_at is not None
    assert (
        timedelta(minutes=4, seconds=55) <= response.expires_at - utc_now() <= timedelta(minutes=5)
    )
    assert "access_key" not in response.model_dump_json()
    assert "secret_key" not in response.model_dump_json()


def test_cache_origin_broker_returns_urls_without_storage_credentials(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context),
        "brokered-worker",
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="workspace-bucket",
            config={
                "access_key": "workspace-access",
                "secret_key": "workspace-secret",
            },
        ),
    )
    image_id = "image-brokered"
    with isolated_services.context.database.session() as session:
        ImageArchiveRepository(session).reserve(
            image_id,
            bucket="image-archives",
            object_key=f"image-archives/{image_id}.rclip",
            size_bytes=1024,
            sha256="a" * 64,
        )
        ImageRepository(session).upsert(ImageRecord(workspace_id=workspace.id, image_id=image_id))
    service = _worker_repository_service(isolated_services, redis)
    service.origin_credentials.config = CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
    )
    service.origin_credentials.archive_settings = _archive_settings()
    archive_storage = _FakeObjectStorage()
    service.origin_credentials.object_store_client = archive_storage
    service.containers.set_container_state(
        SchedulerContainerState(
            container_id="container-brokered",
            stub_id="stub-brokered",
            workspace_id=workspace.id,
            worker_id="worker-1",
            image_id=image_id,
        )
    )

    response = service.get_cache_origin_credentials(
        CacheOriginCredentialRequest(
            workspace_id=workspace.id,
            container_id="container-brokered",
            stub_id="stub-brokered",
            image_id=image_id,
        ),
        principal=WorkerRepositoryPrincipal(
            workspace_id=workspace.id,
            worker_id="worker-1",
        ),
    )

    assert response.credentials is not None
    assert response.credentials.image_archive_url.startswith(
        "memory://image-archives/image-archives/"
    )


def test_cache_origin_broker_denies_other_workers_container_and_image(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "origin-authorization-owner"
    )
    workspace_id = workspace.id
    victim_image_id = "image-victim"
    assigned_image_id = "image-assigned"
    with isolated_services.context.database.session() as session:
        ImageArchiveRepository(session).reserve(
            assigned_image_id,
            bucket="image-archives",
            object_key=f"image-archives/{assigned_image_id}.rclip",
            size_bytes=1024,
            sha256="b" * 64,
        )
        ImageRepository(session).upsert(
            ImageRecord(workspace_id=workspace_id, image_id=assigned_image_id)
        )
    repository = _worker_repository_service(isolated_services, redis)
    repository.origin_credentials.config = CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
    )
    repository.origin_credentials.archive_settings = _archive_settings()
    archive_storage = _FakeObjectStorage()
    repository.origin_credentials.object_store_client = archive_storage
    repository.containers.set_container_state(
        SchedulerContainerState(
            container_id="container-victim",
            stub_id="stub-victim",
            workspace_id=workspace_id,
            worker_id="worker-victim",
            image_id=victim_image_id,
        )
    )
    repository.containers.set_container_state(
        SchedulerContainerState(
            container_id="container-assigned",
            stub_id="stub-victim",
            workspace_id=workspace_id,
            worker_id="worker-attacker",
            image_id=assigned_image_id,
        )
    )
    for status in (SchedulerContainerStatus.Complete, SchedulerContainerStatus.Failed):
        repository.containers.set_container_state(
            SchedulerContainerState(
                container_id=f"container-{status.value}",
                stub_id="stub-victim",
                workspace_id=workspace_id,
                worker_id="worker-attacker",
                image_id=assigned_image_id,
                status=status,
            )
        )
    services = replace(
        isolated_services,
        redis_client=redis,
        binary_redis_client=redis,
        worker_repository_service=repository,
    )
    client = client_stack.enter_context(TestClient(create_app(services)))
    bootstrap_token = _worker_token(isolated_services, "workspace-infrastructure")
    headers = _register_worker_session(
        isolated_services,
        "workspace-infrastructure",
        client,
        bootstrap_token,
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-attacker",
            pool=MachinePool("managed"),
            status=SchedulerWorkerStatus.Available,
        ),
    )

    denied = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id=workspace_id,
            container_id="container-victim",
            stub_id="stub-victim",
            image_id=victim_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    mismatched_image = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id=workspace_id,
            container_id="container-assigned",
            stub_id="stub-victim",
            image_id=victim_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    mismatched_workspace = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id="workspace-other",
            container_id="container-assigned",
            stub_id="stub-victim",
            image_id=assigned_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    mismatched_stub = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id=workspace_id,
            container_id="container-assigned",
            stub_id="stub-other",
            image_id=assigned_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    missing_container = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id=workspace_id,
            container_id="container-missing",
            stub_id="stub-victim",
            image_id=assigned_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    allowed = client.post(
        "/worker-repository/get-cache-origin-credentials",
        json=CacheOriginCredentialRequest(
            workspace_id=workspace_id,
            container_id="container-assigned",
            stub_id="stub-victim",
            image_id=assigned_image_id,
        ).model_dump(mode="json"),
        headers=headers,
    )
    terminal_denials = [
        client.post(
            "/worker-repository/get-cache-origin-credentials",
            json=CacheOriginCredentialRequest(
                workspace_id=workspace_id,
                container_id=f"container-{status.value}",
                stub_id="stub-victim",
                image_id=assigned_image_id,
            ).model_dump(mode="json"),
            headers=headers,
        )
        for status in (SchedulerContainerStatus.Complete, SchedulerContainerStatus.Failed)
    ]

    assert denied.status_code == 403
    assert "image_archive_url" not in denied.text
    assert mismatched_image.status_code == 403
    assert "image_archive_url" not in mismatched_image.text
    assert mismatched_workspace.status_code == 403
    assert "image_archive_url" not in mismatched_workspace.text
    assert mismatched_stub.status_code == 403
    assert "image_archive_url" not in mismatched_stub.text
    assert missing_container.status_code == 403
    assert "image_archive_url" not in missing_container.text
    assert all(response.status_code == 403 for response in terminal_denials)
    assert all("image_archive_url" not in response.text for response in terminal_denials)
    assert allowed.status_code == 200
    credentials = GetCacheOriginCredentialsResponse.model_validate_json(allowed.content).credentials
    assert credentials is not None
    assert credentials.image_archive_url.startswith("memory://image-archives/image-archives/")


def test_worker_repository_api_authenticates_and_streams_container_requests(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    token = _worker_token(isolated_services, "workspace-a")
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-a")
    workspace_owner_user_id(isolated_services.context, workspace.id)
    isolated_services.compute.create_unit(
        UnitName("pool"),
        workspace=workspace.id,
        worker_cpu_millicores=1000,
        worker_memory_mib=1024,
    )
    stub = control.create_stub("worker-request", workspace=workspace.id)
    container_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="worker-request",
                image="",
                command=[],
                workspace_id=workspace.id,
                stub_id=stub.id,
            )
        )
    workers = RedisSchedulerWorkerRepository(redis)
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )
    app = create_app(_api_services(isolated_services, redis))
    client = client_stack.enter_context(TestClient(app))
    headers = _register_worker_session(
        isolated_services,
        workspace.id,
        client,
        token,
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            machine_id="compose-machine",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        ),
    )
    _dispatch_worker_request(
        isolated_services,
        workers,
        RedisSchedulerContainerRepository(redis),
        SchedulerWorkerRequest(
            workspace_id=workspace.id,
            stub_id=stub.id,
            container_id=container_id,
            cpu_millicores=100,
            memory_mib=128,
            pool_selector="pool",
        ),
    )
    worker_lock_key = workers.keys.worker_lock("worker-1")
    assert redis.set(worker_lock_key, "scheduler-owner", ex=10, nx=True)

    unauthenticated = client.post(
        "/worker-repository/get-next-container-request",
        json={
            "worker_id": "worker-1",
            "cache_generation_id": _test_cache_generation_id("worker-1"),
            "cache_session_fence": 1,
            "max_responses": 1,
        },
    )
    authenticated = client.post(
        "/worker-repository/get-next-container-request",
        json={
            "worker_id": "worker-1",
            "cache_generation_id": _test_cache_generation_id("worker-1"),
            "cache_session_fence": 1,
            "max_responses": 1,
        },
        headers=headers,
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert "event: container-request" in authenticated.text
    assert f'"container_id": "{container_id}"' in authenticated.text
    assert redis.get(worker_lock_key) == "scheduler-owner"
    with isolated_services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(container_id)
    assert assigned is not None
    assert assigned.runtime_worker_id == "worker-1"
    assert assigned.runtime_machine_id == "compose-machine"
    assert assigned.worker_id is None
    assert assigned.machine_id is None


def test_worker_network_mutations_are_bound_to_authenticated_worker_assignment(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    services = _api_services(isolated_services, redis)
    repository = services.worker_repository_service
    assert repository is not None
    client = client_stack.enter_context(TestClient(create_app(services)))
    bootstrap_token = _worker_token(isolated_services, "network-owner")
    headers = _register_worker_session(
        isolated_services,
        "network-owner",
        client,
        bootstrap_token,
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-network-owner",
            machine_id="machine-network-owner",
            pool=MachinePool("network-pool"),
            status=SchedulerWorkerStatus.Available,
        ),
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.workspace(session, "network-owner").id
    repository.containers.set_container_state(
        SchedulerContainerState(
            container_id="container-owned",
            stub_id="stub-owned",
            workspace_id=workspace_id,
            worker_id="worker-network-owner",
        )
    )
    repository.containers.set_container_state(
        SchedulerContainerState(
            container_id="container-other-worker",
            stub_id="stub-other",
            workspace_id=workspace_id,
            worker_id="worker-network-other",
        )
    )

    assigned = client.post(
        "/worker-repository/set-container-ip",
        json={"container_id": "container-owned", "ip_address": "10.41.0.2"},
        headers=headers,
    )
    cross_worker = client.post(
        "/worker-repository/set-container-ip",
        json={"container_id": "container-other-worker", "ip_address": "10.41.0.3"},
        headers=headers,
    )
    caller_scoped = client.post(
        "/worker-repository/set-network-lock",
        json={"network_prefix": "another-worker", "ttl_seconds": 5},
        headers=headers,
    )
    locked = client.post(
        "/worker-repository/set-network-lock",
        json={"ttl_seconds": 5},
        headers=headers,
    )

    worker = repository.workers.get_worker("worker-network-owner")
    assert worker is not None
    network_scope = worker_network_prefix(worker.capacity_owner_id, worker.machine_id)
    assert assigned.status_code == 200
    assert cross_worker.status_code == 403
    assert caller_scoped.status_code == 422
    assert locked.status_code == 200
    assert repository.network.get_container_ip(network_scope, "container-owned") == "10.41.0.2"
    assert repository.network.get_container_ip(network_scope, "container-other-worker") is None


def test_worker_repository_stream_blocks_until_scheduler_assignment(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    worker_id = "worker-1"
    container_id = str(uuid4())
    capacity_owner_id = "11111111-1111-4111-8111-111111111111"
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=capacity_owner_id,
            worker_id=worker_id,
            machine_id="compose-machine",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="blocking-worker-stream",
                image="",
                command=[],
                workspace_id=workspace_id,
            )
        )
    isolated_services.compute.create_unit(
        UnitName("default"),
        workspace=workspace_id,
        capacity_owner_id=capacity_owner_id,
        worker_cpu_millicores=1000,
        worker_memory_mib=1024,
    )
    service = _worker_repository_service(isolated_services, redis)
    principal = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id=worker_id,
        token_kind=TokenKind.Worker,
    )
    cache_session = _activate_test_source_cache(service, principal, worker_id)
    stream = service.stream_next_container_requests(
        GetNextContainerRequestRequest(
            worker_id=worker_id,
            cache_generation_id=cache_session.generation_id,
            cache_session_fence=cache_session.session_fence,
            max_responses=1,
        ),
        principal=principal,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(next, stream)
        _dispatch_worker_request(
            isolated_services,
            workers,
            containers,
            SchedulerWorkerRequest(
                workspace_id=workspace_id,
                stub_id=str(uuid4()),
                container_id=container_id,
                cpu_millicores=100,
                memory_mib=128,
                pool_selector="default",
            ),
        )
        response = waiting.result(timeout=1.0)

    assert response.container_request is not None
    assert response.container_request.container_id == container_id
    assert workers.get_next_container_request(worker_id) is None
    with pytest.raises(StopIteration):
        next(stream)


def test_stale_source_cache_session_cannot_change_current_worker_availability(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    worker_id = "worker-1"
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id=worker_id,
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
        )
    )
    service = _worker_repository_service(isolated_services, redis)
    principal = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id=worker_id,
        token_kind=TokenKind.Worker,
    )
    assert service.source_cache is not None
    generation_id = str(uuid4())
    first = service.source_cache.register(
        principal=principal,
        worker_id=worker_id,
        generation_id=generation_id,
        storage_id="node:worker-1",
    )
    service.source_cache.activate(
        principal=principal,
        worker_id=worker_id,
        generation_id=generation_id,
        session_fence=first.session_fence,
    )
    current = service.source_cache.register(
        principal=principal,
        worker_id=worker_id,
        generation_id=generation_id,
        storage_id="node:worker-1",
    )

    stale_request = WorkerCacheSessionRequest(
        worker_id=worker_id,
        cache_generation_id=generation_id,
        cache_session_fence=first.session_fence,
    )
    with pytest.raises(ConflictError, match="no longer current"):
        service.set_worker_keep_alive(stale_request, principal=principal)
    stale_worker = workers.get_worker(worker_id)
    assert stale_worker is not None
    assert stale_worker.status is SchedulerWorkerStatus.Available

    response = service.set_worker_keep_alive(
        WorkerCacheSessionRequest(
            worker_id=worker_id,
            cache_generation_id=generation_id,
            cache_session_fence=current.session_fence,
        ),
        principal=principal,
    )

    assert response.worker is None
    assert response.source_cache_state is WorkerCacheGenerationState.Initializing
    initializing_worker = workers.get_worker(worker_id)
    assert initializing_worker is not None
    assert initializing_worker.status is not SchedulerWorkerStatus.Available


def test_worker_stream_rechecks_cache_after_dequeue_and_requeues_on_drain(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    workers = RedisSchedulerWorkerRepository(redis)
    worker_id = "worker-1"
    workers.add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id=worker_id,
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
        )
    )
    request = SchedulerWorkerRequest(
        workspace_id="workspace-a",
        stub_id="stub-1",
        container_id="container-1",
    )
    workers.enqueue_worker_request(worker_id, request)
    service = _worker_repository_service(isolated_services, redis)
    source_cache = _SecondCheckUnavailableSourceCache(isolated_services.context)
    service.source_cache = source_cache
    principal = WorkerRepositoryPrincipal(
        workspace_id="control-workspace",
        worker_id=worker_id,
        token_kind=TokenKind.Worker,
    )
    cache_session = _activate_test_source_cache(service, principal, worker_id)
    stream = service.stream_next_container_requests(
        GetNextContainerRequestRequest(
            worker_id=worker_id,
            cache_generation_id=cache_session.generation_id,
            cache_session_fence=cache_session.session_fence,
            max_responses=1,
        ),
        principal=principal,
    )

    with pytest.raises(WorkerSourceCacheUnavailableError, match="became draining"):
        next(stream)

    assert source_cache.checks == 2
    assert workers.get_next_container_request(worker_id) == request
    current_worker = workers.get_worker(worker_id)
    assert current_worker is not None
    assert current_worker.status is not SchedulerWorkerStatus.Available


def test_worker_repository_api_vends_container_credentials_from_worker_token(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "workspace-a")
    workspace_owner_user_id(isolated_services.context, workspace.id)
    stub = control.create_stub("worker", workspace=workspace.id)
    isolated_services.secrets.set("API_TOKEN", "secret-value", workspace=workspace.id)
    RedisSchedulerContainerRepository(redis).set_container_state(
        SchedulerContainerState(
            container_id="ctr-1",
            workspace_id=workspace.id,
            stub_id=stub.id,
            worker_id="worker-1",
        )
    )
    token = AuthService(isolated_services.context).create_token(
        "worker-token",
        kind=TokenKind.Worker,
        workspace_id=workspace.id,
        scopes=[AuthScope.Worker.value],
    )[0]
    client = client_stack.enter_context(
        TestClient(create_app(_api_services(isolated_services, redis)))
    )
    headers = _register_worker_session(
        isolated_services,
        workspace.id,
        client,
        token,
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-1",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
        ),
    )
    wrong_worker_headers = _register_worker_session(
        isolated_services,
        workspace.id,
        client,
        token,
        SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id="worker-2",
            pool=MachinePool("pool"),
            status=SchedulerWorkerStatus.Available,
        ),
    )

    wrong_worker_response = client.post(
        "/worker-repository/get-container-credentials",
        json={
            "workspace_id": workspace.id,
            "stub_id": stub.id,
            "container_id": "ctr-1",
            "secret_names": ["API_TOKEN"],
            "gateway_token": True,
        },
        headers=wrong_worker_headers,
    )

    response = client.post(
        "/worker-repository/get-container-credentials",
        json={
            "workspace_id": workspace.id,
            "stub_id": stub.id,
            "container_id": "ctr-1",
            "secret_names": ["API_TOKEN"],
            "gateway_token": True,
        },
        headers=headers,
    )

    assert wrong_worker_response.status_code == 403
    wrong_worker_error = ErrorResponse.model_validate_json(wrong_worker_response.content)
    assert "assigned worker" in wrong_worker_error.detail
    assert response.status_code == 200
    payload = GetContainerCredentialsResponse.model_validate_json(response.content)
    assert payload.credentials is not None
    env = payload.credentials.env
    assert "API_TOKEN=secret-value" in env
    assert any(item.startswith("GATEWAY_TOKEN=rt_") for item in env)


def test_worker_repository_rotates_worker_session_on_reregistration(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    bootstrap = _worker_token(isolated_services, "workspace-a")
    client = client_stack.enter_context(
        TestClient(create_app(_api_services(isolated_services, redis)))
    )
    capacity_owner_id = str(uuid5(NAMESPACE_URL, "lazycloud-test-capacity:workspace-a:pool"))
    isolated_services.compute.create_unit(
        UnitName("pool"),
        workspace="workspace-a",
        provider="local",
        capacity_owner_id=capacity_owner_id,
    )
    payload = _JSON_OBJECT_ADAPTER.validate_python(
        {
            "worker": SchedulerWorkerRecord(
                worker_id="worker-1",
                pool=MachinePool("pool"),
                capacity_owner_id=capacity_owner_id,
                status=SchedulerWorkerStatus.Available,
            ).model_dump(mode="json"),
            "cache_generation_id": _test_cache_generation_id("worker-1"),
            "cache_storage_id": "node:worker-1",
        }
    )
    headers = {"Authorization": f"Bearer {bootstrap}"}

    first = client.post("/worker-repository/add-worker", json=payload, headers=headers)
    second = client.post("/worker-repository/add-worker", json=payload, headers=headers)
    first_token = WorkerRecordResponse.model_validate_json(first.content).worker_session_token
    second_token = WorkerRecordResponse.model_validate_json(second.content).worker_session_token

    rejected = client.post(
        "/worker-repository/acknowledge-worker-event",
        json={"event_id": "event-1", "worker_id": "worker-1"},
        headers={"Authorization": f"Bearer {first_token}"},
    )
    cross_worker = client.post(
        "/worker-repository/stream-worker-events",
        json={"worker_id": "worker-2", "max_events": 1},
        headers={"Authorization": f"Bearer {second_token}"},
    )
    removed = client.post(
        "/worker-repository/remove-worker",
        json={"worker_id": "worker-1"},
        headers={"Authorization": f"Bearer {second_token}"},
    )
    removed_session = client.post(
        "/worker-repository/set-worker-keep-alive",
        json={
            "worker_id": "worker-1",
            "cache_generation_id": _test_cache_generation_id("worker-1"),
            "cache_session_fence": 2,
        },
        headers={"Authorization": f"Bearer {second_token}"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_token != second_token
    assert rejected.status_code == 401
    assert cross_worker.status_code == 403
    assert removed.status_code == 200
    assert removed_session.status_code == 401


def test_worker_registration_fails_closed_without_matching_durable_capacity_owner(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    redis = real_redis_actors.client()
    bootstrap = _worker_token(isolated_services, "capacity-owner-registration")
    client = client_stack.enter_context(
        TestClient(create_app(_api_services(isolated_services, redis)))
    )
    owner_id = str(uuid5(NAMESPACE_URL, "capacity-owner-registration:pool"))
    other_owner_id = str(uuid5(NAMESPACE_URL, "capacity-owner-registration:other"))
    base_payload = {
        "cache_generation_id": _test_cache_generation_id("capacity-worker"),
        "cache_storage_id": "machine:capacity-worker-machine",
    }
    headers = {"Authorization": f"Bearer {bootstrap}"}

    missing_identity = client.post(
        "/worker-repository/add-worker",
        json={
            **base_payload,
            "worker": {
                "worker_id": "capacity-worker",
                "pool": "capacity-pool",
            },
        },
        headers=headers,
    )
    missing_pool = client.post(
        "/worker-repository/add-worker",
        json={
            **base_payload,
            "worker": SchedulerWorkerRecord(
                worker_id="capacity-worker",
                pool=MachinePool("capacity-pool"),
                capacity_owner_id=owner_id,
            ).model_dump(mode="json"),
        },
        headers=headers,
    )
    isolated_services.compute.create_unit(
        UnitName("capacity-pool"),
        workspace="capacity-owner-registration",
        provider="local",
        capacity_owner_id=owner_id,
    )
    owner_mismatch = client.post(
        "/worker-repository/add-worker",
        json={
            **base_payload,
            "worker": SchedulerWorkerRecord(
                worker_id="capacity-worker",
                pool=MachinePool("capacity-pool"),
                capacity_owner_id=other_owner_id,
            ).model_dump(mode="json"),
        },
        headers=headers,
    )
    accepted = client.post(
        "/worker-repository/add-worker",
        json={
            **base_payload,
            "worker": SchedulerWorkerRecord(
                worker_id="capacity-worker",
                pool=MachinePool("capacity-pool"),
                capacity_owner_id=owner_id,
            ).model_dump(mode="json"),
        },
        headers=headers,
    )

    assert missing_identity.status_code == 422
    assert "capacity_owner_id" in missing_identity.text
    assert missing_pool.status_code == 503
    assert "capacity pool is unavailable" in missing_pool.text
    assert owner_mismatch.status_code == 409
    assert "capacity owner does not match" in owner_mismatch.text
    assert accepted.status_code == 200


def test_container_shutdown_owner_confirms_targeted_worker_ack(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    containers = RedisSchedulerContainerRepository(redis)
    containers.set_container_state(
        SchedulerContainerState(
            container_id="ctr-1",
            workspace_id="workspace-a",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )
    service = ContainerShutdownService(
        containers,
        RedisEventBus(redis),
        redis,
        poll_interval_seconds=0.001,
    )
    event_id = event_id_for_event(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": "ctr-1",
                "force": False,
                "reason": StopContainerReason.User.value,
                "worker_id": "worker-1",
            },
            retries=3,
        )
    )

    def acknowledge() -> None:
        containers.delete_container_state("ctr-1")
        redis.set_add(redis.key("worker-events", "ack", event_id), "worker-1")

    timer = threading.Timer(0.01, acknowledge)
    timer.start()
    service.confirm(
        [ContainerShutdownTarget(container_id="ctr-1", worker_id="worker-1")],
        timeout_seconds=0.5,
    )
    timer.join()

    assert redis.set_members(redis.key("worker-events", "pending", "worker-1")) == set()
    assert redis.set_members(redis.key("worker-events", "ack", event_id)) == set()
    assert redis.exists(redis.key("event", event_id)) == 0


def test_container_shutdown_owner_accepts_concurrent_scheduler_completion(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    containers = RedisSchedulerContainerRepository(redis)
    containers.set_container_state(
        SchedulerContainerState(
            container_id="ctr-concurrent-complete",
            workspace_id="workspace-a",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )
    service = ContainerShutdownService(
        containers,
        RedisEventBus(redis),
        redis,
        poll_interval_seconds=0.001,
    )
    event_id = event_id_for_event(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": "ctr-concurrent-complete",
                "force": False,
                "reason": StopContainerReason.User.value,
                "worker_id": "worker-1",
            },
            retries=3,
        )
    )

    timer = threading.Timer(
        0.01,
        lambda: containers.update_container_status(
            "ctr-concurrent-complete",
            SchedulerContainerStatus.Complete,
        ),
    )
    timer.start()
    service.confirm(
        [
            ContainerShutdownTarget(
                container_id="ctr-concurrent-complete",
                worker_id="worker-1",
            )
        ],
        timeout_seconds=0.5,
    )
    timer.join()

    assert redis.set_members(redis.key("worker-events", "pending", "worker-1")) == set()
    assert redis.set_members(redis.key("worker-events", "ack", event_id)) == set()
    assert redis.exists(redis.key("event", event_id)) == 0


def test_container_shutdown_owner_accepts_unassigned_pending_cancellation(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    containers = RedisSchedulerContainerRepository(redis)
    containers.set_container_state(
        SchedulerContainerState(
            container_id="ctr-pending-unassigned",
            workspace_id="workspace-a",
            stub_id="stub-1",
            status=SchedulerContainerStatus.Pending,
        )
    )
    containers.cancel_container_request("ctr-pending-unassigned")
    service = ContainerShutdownService(
        containers,
        RedisEventBus(redis),
        redis,
        poll_interval_seconds=0.001,
    )

    service.confirm(
        [ContainerShutdownTarget(container_id="ctr-pending-unassigned")],
        timeout_seconds=0.05,
    )

    assert redis.scan(redis.key("event", "*")) == []
    assert redis.scan(redis.key("worker-events", "pending", "*")) == []
    assert redis.scan(redis.key("worker-events", "ack", "*")) == []


def test_container_shutdown_owner_rejects_optimistic_database_terminal_state(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    containers = RedisSchedulerContainerRepository(redis)
    container_id = str(uuid4())
    workspace = owned_workspace(
        ControlPlaneService(isolated_services.context), "shutdown-finalization"
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            workspace_id=workspace.id,
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="finalized",
                image="",
                command=[],
                workspace_id=workspace.id,
                status=ContainerStatus.Running,
            )
        )
    service = ContainerShutdownService(
        containers,
        RedisEventBus(redis),
        redis,
        poll_interval_seconds=0.001,
    )
    event_id = event_id_for_event(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": container_id,
                "force": False,
                "reason": StopContainerReason.User.value,
                "worker_id": "worker-1",
            },
            retries=3,
        )
    )

    def finalize_without_acknowledgement() -> None:
        with isolated_services.context.database.session() as session:
            ContainerRepository(session).upsert(
                ContainerRecord(
                    id=container_id,
                    name="finalized",
                    image="",
                    command=[],
                    workspace_id=workspace.id,
                    status=ContainerStatus.Stopped,
                )
            )
        containers.delete_container_state(container_id)

    timer = threading.Timer(0.01, finalize_without_acknowledgement)
    timer.start()
    with pytest.raises(UpstreamUnavailableError, match="workers=worker-1"):
        service.confirm(
            [ContainerShutdownTarget(container_id=container_id, worker_id="worker-1")],
            timeout_seconds=0.05,
        )
    timer.join()

    assert redis.set_members(redis.key("worker-events", "pending", "worker-1")) == {event_id}
    assert redis.set_members(redis.key("worker-events", "ack", event_id)) == set()
    assert redis.exists(redis.key("event", event_id)) == 1


def test_container_shutdown_owner_rejects_cross_worker_shutdown_ack(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    containers = RedisSchedulerContainerRepository(redis)
    containers.set_container_state(
        SchedulerContainerState(
            container_id="ctr-1",
            workspace_id="workspace-a",
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )
    service = ContainerShutdownService(
        containers,
        RedisEventBus(redis),
        redis,
        poll_interval_seconds=0.001,
    )
    event_id = event_id_for_event(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": "ctr-1",
                "force": False,
                "reason": StopContainerReason.User.value,
                "worker_id": "worker-1",
            },
            retries=3,
        )
    )

    def acknowledge_as_other_worker() -> None:
        redis.set_add(redis.key("worker-events", "ack", event_id), "worker-2")

    timer = threading.Timer(0.01, acknowledge_as_other_worker)
    timer.start()
    with pytest.raises(UpstreamUnavailableError, match="workers=worker-1"):
        service.confirm(
            [ContainerShutdownTarget(container_id="ctr-1", worker_id="worker-1")],
            timeout_seconds=0.05,
        )
    timer.join()


def test_worker_repository_filters_targeted_stop_events_by_assigned_worker(
    isolated_services: ApiServices,
) -> None:
    fake = FakeRedis()
    redis = RedisClient(fake, key_prefix="test")
    service = _worker_repository_service(isolated_services, redis)
    sent = service.events.send(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": "ctr-1",
                "worker_id": "worker-1",
                "reason": StopContainerReason.User.value,
            },
        )
    )

    assert service._event_targets_worker(sent.event_id, "worker-1")
    assert not service._event_targets_worker(sent.event_id, "worker-2")

    untargeted = service.events.send(
        EventBusEvent(
            type=EventBusEventType.StopContainer,
            args={
                "container_id": "ctr-2",
                "reason": StopContainerReason.User.value,
            },
        )
    )
    assert not service._event_targets_worker(untargeted.event_id, "worker-1")


def test_worker_repository_service_persists_checkpoint_archive_and_state(
    isolated_services: ApiServices,
) -> None:
    fake = FakeRedis()
    redis = RedisClient(fake, key_prefix="test")
    object_storage = _FakeObjectStorage()
    cache_storage = _FakeCacheStorage()
    service = _worker_repository_service(
        isolated_services,
        redis,
        object_storage=object_storage,
        cache_storage=cache_storage,
    )
    archive = b"checkpoint-archive"
    archive_hash = hashlib.sha256(archive).hexdigest()
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "default")
    stub = control.create_stub("checkpoint-archive", workspace=workspace.id)
    service.save_checkpoint_state(
        SaveCheckpointStateRequest(
            payload=CheckpointStatePayload(
                operation=CheckpointStateOperation.Create,
                checkpoint_id="checkpoint-1",
                status=WorkerCheckpointStatus.Pending,
                workspace_id=workspace.id,
                stub_id=stub.id,
                stub_type="sandbox",
            )
        )
    )

    prepared = service.prepare_checkpoint_archive_upload(
        PrepareCheckpointArchiveUploadRequest(
            checkpoint_id="checkpoint-1",
            origin_key="checkpoints/checkpoint-1.tar",
            cache_hash=archive_hash,
            cache_size_bytes=len(archive),
            checkpoint_bucket="checkpoint-bucket",
        )
    )
    object_storage.files[("checkpoint-bucket", "checkpoints/checkpoint-1.tar")] = archive
    persisted = service.persist_checkpoint_archive(
        PersistCheckpointArchiveRequest(
            checkpoint_id="checkpoint-1",
            origin_key="checkpoints/checkpoint-1.tar",
            cache_hash=archive_hash,
            cache_size_bytes=len(archive),
            checkpoint_bucket="checkpoint-bucket",
            cache_namespace="checkpoints",
            locality="pool-a",
            accelerator="gpu-a",
        )
    )
    saved = service.save_checkpoint_state(
        SaveCheckpointStateRequest(
            payload=CheckpointStatePayload(
                operation=CheckpointStateOperation.Create,
                checkpoint_id="checkpoint-1",
                status=WorkerCheckpointStatus.Available,
                workspace_id=workspace.id,
                stub_id=stub.id,
                stub_type="sandbox",
                cache_hash=persisted.cache_hash,
                cache_size_bytes=persisted.cache_size_bytes,
                origin_key=persisted.origin_key,
                locality=persisted.locality,
                accelerator=persisted.accelerator,
            )
        )
    )
    restore = service.get_checkpoint_restore(
        GetCheckpointRestoreRequest(
            checkpoint_id="checkpoint-1",
            workspace_id=workspace.id,
            checkpoint_bucket="checkpoint-bucket",
        )
    )

    with isolated_services.context.database.session() as session:
        checkpoint = CheckpointRepository(session).get_across_workspaces("checkpoint-1")
    assert persisted.cache_hash == archive_hash
    assert prepared.upload_url.startswith(
        f"memory://checkpoint-bucket/workspaces/{workspace.id}/checkpoint-bucket/"
    )
    assert object_storage.files[("checkpoint-bucket", "checkpoints/checkpoint-1.tar")] == archive
    assert cache_storage.files[("checkpoints", archive_hash)] == archive
    assert saved.checkpoint is not None
    assert checkpoint is not None
    assert checkpoint.status.value == WorkerCheckpointStatus.Available.value
    assert checkpoint.origin_key == "checkpoints/checkpoint-1.tar"
    assert restore.checkpoint == checkpoint
    assert restore.download_url.startswith(
        f"memory://checkpoint-bucket/workspaces/{workspace.id}/checkpoint-bucket/"
    )
    assert object_storage.workspace_put_urls == [
        (workspace.id, "checkpoint-bucket", "checkpoints/checkpoint-1.tar")
    ]
    assert object_storage.workspace_get_urls == [
        (workspace.id, "checkpoint-bucket", "checkpoints/checkpoint-1.tar")
    ]


def test_worker_repository_lifecycle_failure_marks_container_and_task_failed(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    task = isolated_services.tasks.create("startup-task")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).records.create(
            {
                "name": "function-startup-task",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runtime"],
                "workspace_id": workspace_id,
                "task_id": task.id,
                "status": ContainerStatus.Pending.value,
            },
            workspace_id=workspace_id,
            name="function-startup-task",
            status=ContainerStatus.Pending.value,
        )
    containers = RedisSchedulerContainerRepository(redis)
    containers.set_container_state(
        SchedulerContainerState(
            container_id=container.id,
            workspace_id=workspace_id,
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Pending,
        )
    )

    now = utc_now()
    response = service.publish_container_lifecycle(
        PublishContainerLifecycleRequest(
            payload=ContainerLifecyclePayload(
                id="load-image",
                container_id=container.id,
                workspace_id=workspace_id,
                task_id=task.id,
                worker_id="worker-1",
                start_time=now,
                end_time=now,
                duration_ms=12,
                success=False,
                attrs={"error": "image archive missing"},
            )
        )
    )

    state = containers.get_container_state(container.id)
    with isolated_services.context.database.session() as session:
        updated_container = ContainerRepository(session).get_across_workspaces(container.id)
        updated_task = TaskRepository(session).get_across_workspaces(task.id)
    assert response.event is not None
    assert state is not None
    assert state.status is SchedulerContainerStatus.Failed
    assert updated_container is not None
    assert updated_container.status is ContainerStatus.Failed
    assert updated_container.exit_code == 1
    assert updated_container.finished_at is not None
    assert updated_task is not None
    assert updated_task.status is TaskStatus.Failed
    assert updated_task.exit_code == 1
    assert updated_task.kwargs["container_id"] == container.id
    assert updated_task.error == (
        "container startup failed during load-image: image archive missing"
    )


def test_worker_repository_exit_preserves_function_retry_state(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    task = isolated_services.tasks.create("function-retry")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).records.create(
            {
                "name": "function-retry",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runner.function"],
                "workspace_id": workspace_id,
                "task_id": task.id,
                "runtime_worker_id": "worker-1",
                "status": ContainerStatus.Running.value,
            },
            workspace_id=workspace_id,
            name="function-retry",
            status=ContainerStatus.Running.value,
        )
    task.container_id = container.id
    task.status = TaskStatus.Retry
    isolated_services.tasks.save(task)

    service.set_container_exit_code(
        SetContainerExitCodeRequest(container_id=container.id, exit_code=1),
        principal=WorkerRepositoryPrincipal(worker_id="worker-1"),
    )

    updated = isolated_services.tasks.get(task.id)
    assert updated.status is TaskStatus.Retry
    assert updated.exit_code is None


def test_worker_repository_stale_container_exit_does_not_fail_new_attempt(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    task = isolated_services.tasks.create("function-new-attempt")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        old_container = ContainerRepository(session).records.create(
            {
                "name": "function-old-attempt",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runner.function"],
                "workspace_id": workspace_id,
                "task_id": task.id,
                "runtime_worker_id": "worker-1",
                "status": ContainerStatus.Running.value,
            },
            workspace_id=workspace_id,
            name="function-old-attempt",
            status=ContainerStatus.Running.value,
        )
        new_container = ContainerRepository(session).records.create(
            {
                "name": "function-new-attempt",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runner.function"],
                "workspace_id": workspace_id,
                "task_id": task.id,
                "status": ContainerStatus.Running.value,
            },
            workspace_id=workspace_id,
            name="function-new-attempt",
            status=ContainerStatus.Running.value,
        )
    task.container_id = new_container.id
    task.status = TaskStatus.Running
    isolated_services.tasks.save(task)

    service.set_container_exit_code(
        SetContainerExitCodeRequest(container_id=old_container.id, exit_code=1),
        principal=WorkerRepositoryPrincipal(worker_id="worker-1"),
    )

    updated = isolated_services.tasks.get(task.id)
    assert updated.status is TaskStatus.Running
    assert updated.container_id == new_container.id
    assert updated.exit_code is None


def test_worker_repository_late_exit_preserves_user_stopped_container(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    task = isolated_services.tasks.create("stopped-task")
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).records.create(
            {
                "name": "preview-function",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runner.function"],
                "workspace_id": workspace_id,
                "task_id": task.id,
                "status": ContainerStatus.Running.value,
            },
            workspace_id=workspace_id,
            name="preview-function",
            status=ContainerStatus.Running.value,
        )
    containers = RedisSchedulerContainerRepository(redis)
    container_service = replace(
        isolated_services.containers,
        scheduler_cancellation=scheduler_request_service_for_redis(
            isolated_services,
            redis,
            containers=containers,
        ),
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id=container.id,
            workspace_id=workspace_id,
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )

    stopped = container_service.stop(container.id)
    service.set_container_exit_code(
        SetContainerExitCodeRequest(
            container_id=container.id,
            exit_code=137,
            termination_reason=StopContainerReason.Preempted,
        ),
        principal=WorkerRepositoryPrincipal(worker_id="worker-1"),
    )
    service.update_container_status(
        UpdateContainerStatusRequest(
            container_id=container.id,
            status=SchedulerContainerStatus.Failed,
        ),
        principal=WorkerRepositoryPrincipal(worker_id="worker-1"),
    )

    with isolated_services.context.database.session() as session:
        updated_container = ContainerRepository(session).get_across_workspaces(container.id)
        updated_task = TaskRepository(session).get_across_workspaces(task.id)
    assert stopped.status is ContainerStatus.Stopped
    assert updated_container is not None
    assert updated_container.status is ContainerStatus.Stopped
    assert updated_container.exit_code == 137
    assert updated_container.termination_reason is StopContainerReason.Preempted
    assert containers.get_termination_reason(container.id) is StopContainerReason.Preempted
    assert updated_container.finished_at is not None
    assert updated_task is not None
    assert updated_task.status is TaskStatus.Cancelled
    assert updated_task.exit_code is None


def test_worker_repository_container_cleanup_unpublishes_every_port_route(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    containers = RedisSchedulerContainerRepository(redis)
    compute_states = RedisComputeStateRepository(redis)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=_ROUTE_CAPACITY_OWNER,
            worker_id="compose-container-worker",
            workspace_id=workspace_id,
            machine_id="compose-machine",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )
    container_id = "531a1b89-6f97-4080-80b3-13122218a55b"
    containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            workspace_id=workspace_id,
            stub_id="stub-1",
            worker_id="compose-container-worker",
            status=SchedulerContainerStatus.Running,
        )
    )
    routes = [
        AgentBackendRoute(
            route_id=f"compose-machine:compose-container-worker:{container_id}:container:{port}",
            workspace_id=workspace_id,
            pool=MachinePool("default"),
            machine_id="compose-machine",
            worker_id="compose-container-worker",
            container_id=container_id,
            port=port,
            transport=BackendRouteTransport.Direct,
            local_target=f"10.0.0.2:{port}",
            proxy_target=f"10.0.0.2:{port}",
            state=BackendRouteState.Ready,
        )
        for port in (8080, 9090, 2222)
    ]
    service.set_container_address(
        SetContainerAddressRequest(
            container_id=container_id,
            address=routes[0].local_target,
            route=routes[0],
        )
    )
    service.set_container_address_map(
        SetContainerAddressMapRequest(
            container_id=container_id,
            address_map={route.port: route.local_target for route in routes},
            routes=routes,
        )
    )
    containers.set_worker_address(
        container_id,
        "10.0.0.2:9000",
        route=routes[0],
    )
    service.set_container_exit_code(
        SetContainerExitCodeRequest(container_id=container_id, exit_code=0),
        principal=WorkerRepositoryPrincipal(worker_id="compose-container-worker"),
    )

    before = compute_states.list_agent_route_states(
        workspace_id,
        _ROUTE_CAPACITY_OWNER,
        "compose-machine",
    )
    response = service.delete_container_state(
        DeleteContainerStateRequest(container_id=container_id),
        principal=WorkerRepositoryPrincipal(worker_id="compose-container-worker"),
    )

    assert {route.route_id for route in before} == {route.route_id for route in routes}
    assert response.deleted
    assert containers.get_container_state(container_id) is None
    assert containers.get_container_address_map(container_id).routes == []
    assert (
        compute_states.list_agent_route_states(
            workspace_id,
            _ROUTE_CAPACITY_OWNER,
            "compose-machine",
        )
        == []
    )
    assert containers.get_exit_code(container_id) == 0


def test_worker_repository_reconciles_orphan_routes_without_removing_active_routes(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    containers = RedisSchedulerContainerRepository(redis)
    compute_states = RedisComputeStateRepository(redis)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=_ROUTE_CAPACITY_OWNER,
            worker_id="worker-1",
            workspace_id=workspace_id,
            machine_id="machine-1",
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )
    container_id = "531a1b89-6f97-4080-80b3-13122218a55b"
    active_route = AgentBackendRoute(
        route_id=f"machine-1:worker-1:{container_id}:container:9090",
        workspace_id=workspace_id,
        pool=MachinePool("default"),
        machine_id="machine-1",
        worker_id="worker-1",
        container_id=container_id,
        port=9090,
        transport=BackendRouteTransport.Direct,
        local_target="10.0.0.2:9090",
        proxy_target="10.0.0.2:9090",
        state=BackendRouteState.Ready,
    )
    containers.set_container_state(
        SchedulerContainerState(
            container_id=container_id,
            workspace_id=workspace_id,
            stub_id="stub-1",
            worker_id="worker-1",
            status=SchedulerContainerStatus.Running,
        )
    )
    service.set_container_address_map(
        SetContainerAddressMapRequest(
            container_id=container_id,
            address_map={9090: active_route.local_target},
            routes=[active_route],
        )
    )
    orphan_route = AgentBackendRoute(
        route_id="missing-worker:missing-container:container:9090",
        workspace_id=workspace_id,
        capacity_owner_id=_ROUTE_CAPACITY_OWNER,
        pool=MachinePool("default"),
        machine_id="machine-1",
        worker_id="missing-worker",
        container_id="missing-container",
        port=9090,
        state=BackendRouteState.Ready,
    )
    compute_states.save_agent_route_state(orphan_route)

    result = service.reconcile_orphan_agent_routes()

    assert result.scanned == 2
    assert result.removed == 1
    assert (
        compute_states.get_agent_route_state(
            workspace_id,
            _ROUTE_CAPACITY_OWNER,
            "machine-1",
            active_route.route_id,
        )
        is not None
    )
    assert (
        compute_states.get_agent_route_state(
            workspace_id,
            _ROUTE_CAPACITY_OWNER,
            "machine-1",
            orphan_route.route_id,
        )
        is None
    )


def test_agent_route_status_update_reconciles_scheduler_backend_route(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    compute_states = RedisComputeStateRepository(redis)
    containers = RedisSchedulerContainerRepository(redis)
    gateway = replace(
        isolated_services.gateway_service,
        compute_state=compute_states,
        scheduler_workers=RedisSchedulerWorkerRepository(redis),
        scheduler_containers=containers,
        scheduler_pool_states=RedisWorkerPoolStateRepository(redis),
        tailnet=TailnetConfig(),
        tailnet_control=_WorkerRepositoryTailnetControl(),
    )
    workspace_id, machine_id, agent_token = _join_gateway_agent(
        isolated_services,
        gateway,
        pool=MachinePool("pool-a"),
        machine_fingerprint="route-machine",
    )
    worker_id = agent_machine_worker_id(machine_id)
    # Routes are filed under the machine's own unit, so the worker record has to name
    # the unit the agent actually joined rather than one invented here.
    joined_unit = gateway.unit_state_coordinator.unit_by_name(
        UnitName(MachinePool("pool-a")),
        workspace_id=workspace_id,
    )
    RedisSchedulerWorkerRepository(redis).add_worker(
        SchedulerWorkerRecord(
            capacity_owner_id=joined_unit.capacity_owner_id,
            worker_id=worker_id,
            workspace_id=workspace_id,
            machine_id=machine_id,
            pool=MachinePool("pool-a"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        )
    )
    route = AgentBackendRoute(
        route_id=f"{machine_id}:{worker_id}:container-1:container:8001",
        workspace_id=workspace_id,
        pool=MachinePool("pool-a"),
        machine_id=machine_id,
        worker_id=worker_id,
        container_id="container-1",
        port=8001,
        transport=BackendRouteTransport.TsnetRestricted,
        local_target="192.168.0.4:8001",
        state=BackendRouteState.Opening,
    )
    service = _worker_repository_service(isolated_services, redis)
    service.set_container_address(
        SetContainerAddressRequest(
            container_id="container-1",
            address="192.168.0.4:8001",
            route=route,
        )
    )
    response = gateway.update_agent_route_status(
        UpdateAgentRouteStatusRequest(
            agent_token=agent_token,
            route_id=route.route_id,
            state=BackendRouteState.Ready,
            proxy_target="tailnet-host:34399",
        )
    )
    resolved = SchedulerBackendRouteResolver(
        isolated_services.routes,
        containers,
    ).get_backend_route(route.route_id)

    assert response.route_id == route.route_id
    assert resolved is not None
    assert resolved.state == BackendRouteState.Ready.value
    assert resolved.proxy_target == "tailnet-host:34399"


def _join_gateway_agent(
    services: ApiServices,
    gateway: GatewayControlService,
    *,
    pool: MachinePool,
    machine_fingerprint: str,
) -> tuple[str, str, str]:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    # The join credential names the account the machine belongs to, so the workspace
    # needs the owner row production writes with it.
    workspace_owner_user_id(services.context, workspace_id)
    services.compute.create_unit(
        UnitName(pool),
        provider="agent",
        workspace=workspace_id,
    )
    bootstrap = gateway.unit_state_coordinator.create_unit_join_token(
        gateway.unit_state_coordinator.unit_by_name(UnitName(pool), workspace_id=workspace_id),
        workspace_id=workspace_id,
        owner_token_id="worker-repository-test",
    )
    joined = gateway.join_agent(
        JoinAgentRequest(
            join_token=bootstrap.token,
            machine_fingerprint=machine_fingerprint,
            hostname=machine_fingerprint,
            os="linux",
            arch="amd64",
            cpu_count=2,
            memory_mb=4096,
            preflight=[
                ComputePreflightCheck(
                    name="container-runtime",
                    ok=True,
                    severity=PreflightSeverity.Error,
                )
            ],
        )
    )
    gateway.request_agent_transport_credential(
        RequestAgentTransportCredentialRequest(
            agent_token=joined.agent_token,
            transport=BackendRouteTransport.TsnetRestricted,
        )
    )
    gateway.register_agent_tailnet_device(
        RegisterAgentTailnetDeviceRequest(
            agent_token=joined.agent_token,
            node_id=f"node-{joined.machine_id}",
        )
    )
    return workspace_id, joined.machine_id, joined.agent_token


_ROUTE_CAPACITY_OWNER = "33333333-3333-4333-8333-333333333333"
"""Capacity owner the route tests register their worker under.

Routes are filed under the machine's owner read from its worker record, so a read
that names anything else finds nothing.
"""


def _worker_token(
    isolated_services: ApiServices,
    workspace_id: str,
    *,
    kind: TokenKind = TokenKind.Worker,
) -> str:
    workspace = owned_workspace(ControlPlaneService(isolated_services.context), workspace_id)
    workspace_owner_user_id(isolated_services.context, workspace.id)
    return AuthService(isolated_services.context).create_token(
        f"{workspace_id}-{kind.value}",
        kind=kind,
        workspace_id=workspace_id,
        scopes=[AuthScope.Worker.value],
    )[0]


def _register_worker_session(
    services: ApiServices,
    workspace_id: str,
    client: TestClient,
    bootstrap_token: str,
    worker: SchedulerWorkerRecord,
) -> dict[str, str]:
    with services.context.database.session() as session:
        durable_workspace_id = services.context.workspace(session, workspace_id).id
        unit = ComputeUnitRepository(session).get_by_name(
            durable_workspace_id,
            worker.pool,
        )
    if unit is None:
        capacity_owner_id = str(
            uuid5(
                NAMESPACE_URL,
                f"lazycloud-test-capacity:{durable_workspace_id}:{worker.pool}",
            )
        )
        unit = services.compute.create_unit(
            UnitName(worker.pool),
            workspace=durable_workspace_id,
            provider="local",
            capacity_owner_id=capacity_owner_id,
        )
    registered_worker = worker.model_copy(update={"capacity_owner_id": unit.capacity_owner_id})
    generation_id = _test_cache_generation_id(worker.worker_id)
    response = client.post(
        "/worker-repository/add-worker",
        json={
            "worker": registered_worker.model_dump(mode="json"),
            "cache_generation_id": generation_id,
            "cache_storage_id": f"node:{worker.worker_id}",
        },
        headers={"Authorization": f"Bearer {bootstrap_token}"},
    )
    assert response.status_code == 200
    registration = WorkerRecordResponse.model_validate_json(response.content)
    session_token = registration.worker_session_token
    assert session_token.startswith("rt_")
    assert registration.cache_session is not None
    headers = {"Authorization": f"Bearer {session_token}"}
    activated = client.post(
        "/worker-repository/toggle-worker-available",
        json={
            "worker_id": worker.worker_id,
            "cache_generation_id": registration.cache_session.generation_id,
            "cache_session_fence": registration.cache_session.session_fence,
        },
        headers=headers,
    )
    assert activated.status_code == 200
    return headers


def _test_cache_generation_id(worker_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"lazycloud-test-cache:{worker_id}"))


def _activate_test_source_cache(
    service: WorkerRepositoryService,
    principal: WorkerRepositoryPrincipal,
    worker_id: str,
) -> WorkerCacheSession:
    assert service.source_cache is not None
    generation = service.source_cache.register(
        principal=principal,
        worker_id=worker_id,
        generation_id=str(uuid4()),
        storage_id=f"node:{worker_id}",
    )
    activated = service.source_cache.activate(
        principal=principal,
        worker_id=worker_id,
        generation_id=generation.id,
        session_fence=generation.session_fence,
    )
    return WorkerCacheSession(
        generation_id=activated.id,
        session_fence=activated.session_fence,
    )


class _SecondCheckUnavailableSourceCache(WorkerSourceCacheService):
    def __init__(self, context: ServiceContext) -> None:
        super().__init__(context)
        self.checks = 0

    def require_available(
        self,
        *,
        principal: WorkerRepositoryPrincipal,
        worker_id: str,
        generation_id: str,
        session_fence: int,
    ) -> WorkerCacheGenerationRecord:
        generation = super().require_available(
            principal=principal,
            worker_id=worker_id,
            generation_id=generation_id,
            session_fence=session_fence,
        )
        self.checks += 1
        if self.checks == 2:
            raise WorkerSourceCacheUnavailableError("source cache became draining")
        return generation


def _dispatch_worker_request(
    isolated_services: ApiServices,
    workers: RedisSchedulerWorkerRepository,
    containers: RedisSchedulerContainerRepository,
    request: SchedulerWorkerRequest,
) -> None:
    scheduler = scheduler_request_service_for_redis(
        isolated_services,
        workers.redis,
        workers=workers,
        containers=containers,
    )
    assert scheduler.submit(request).accepted
    [result] = scheduler.dispatch_ready(limit=1)
    assert result.status is SchedulerContainerDispatchStatus.Dispatched


def _worker_repository_service(
    isolated_services: ApiServices,
    redis: RedisClient,
    *,
    object_storage: WorkerRepositoryObjectStorage | None = None,
    cache_storage: WorkerRepositoryCacheStorage | None = None,
) -> WorkerRepositoryService:
    containers = RedisSchedulerContainerRepository(redis)
    return WorkerRepositoryService(
        workers=RedisSchedulerWorkerRepository(redis),
        containers=containers,
        network=RedisWorkerNetworkIpRepository(redis),
        events=RedisEventBus(redis),
        container_credentials=WorkerCredentialService(
            services=isolated_services,
            container_repository=containers,
        ),
        origin_credentials=WorkerCacheOriginCredentialService(services=isolated_services),
        dependencies=WorkerRepositoryDependencies(
            context=isolated_services.context,
            auth=isolated_services.auth,
            deployment_resources=isolated_services.deployment_resources,
            checkpoints=isolated_services.checkpoints,
            images=isolated_services.images,
            object_storage=object_storage or isolated_services.object_storage,
            worker_events=isolated_services.worker_events,
            usage=isolated_services.usage,
            workspace_changes=isolated_services.workspace_changes,
            preempted_containers=PreemptedContainerService(
                services=isolated_services,
                stubs=isolated_services.control_plane_service,
            ),
        ),
        redis=redis,
        object_storage=object_storage,
        cache_storage=cache_storage,
        source_cache=WorkerSourceCacheService(isolated_services.context),
    )


def _api_services(isolated_services: ApiServices, redis: RedisClient) -> ApiServices:
    return replace(
        isolated_services,
        redis_client=redis,
        binary_redis_client=redis,
        worker_repository_service=_worker_repository_service(isolated_services, redis),
    )


def _archive_settings() -> ResolvedImageArchiveSettings:
    return ResolvedImageArchiveSettings(
        storage=S3ObjectStoreSettings(bucket="image-archives"),
        prefix="",
        presign_seconds=3600,
    )


class _FakeObjectStorage:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}
        self.workspace_get_urls: list[tuple[str, str, str]] = []
        self.workspace_put_urls: list[tuple[str, str, str]] = []

    @property
    def object_client(self) -> _FakeObjectStorage:
        return self

    def put_file(self, bucket: str, key: str, source: str | Path) -> ObjectRecord:
        payload = Path(source).read_bytes()
        self.files[(bucket, key)] = payload
        return _fake_object_record(bucket, key, payload)

    def get(self, bucket: str, key: str) -> ObjectRecord:
        return _fake_object_record(bucket, key, self.files[(bucket, key)])

    def get_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
    ) -> ObjectRecord:
        _ = workspace_id
        return self.get(bucket, key)

    def get_by_id_for_workspace(
        self,
        object_id: str,
        *,
        workspace_id: str,
    ) -> ObjectRecord:
        del object_id, workspace_id
        raise KeyError("fake object records are not configured")

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        return (bucket or "default", key) in self.files

    def head(self, key: str, *, bucket: str | None = None) -> S3ObjectInfo:
        resolved_bucket = bucket or "default"
        payload = self.files.get((resolved_bucket, key))
        if payload is None:
            raise KeyError(f"{resolved_bucket}/{key}")
        return S3ObjectInfo(
            bucket=resolved_bucket,
            key=key,
            size=len(payload),
            metadata={"artifact-sha256": hashlib.sha256(payload).hexdigest()},
        )

    def generate_presigned_get_url(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
    ) -> str:
        return f"memory://{bucket or 'default'}/{key}?expires={expires_seconds}"

    def generate_presigned_get_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        self.workspace_get_urls.append((workspace_id, bucket, key))
        return self.generate_presigned_get_url(
            f"workspaces/{workspace_id}/{bucket}/{key}",
            bucket=bucket,
            expires_seconds=expires_seconds,
        )

    def reserve(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord:
        del overwrite
        return ObjectRecord(
            id=f"{bucket}/{key}",
            bucket=bucket,
            key=key,
            path=f"{bucket}/{key}",
            size=size,
            sha256=sha256,
            content_type=content_type,
            metadata=dict(metadata or {}),
        )

    def reserve_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        size: int,
        sha256: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ObjectRecord:
        _ = workspace_id
        return self.reserve(
            bucket,
            key,
            size=size,
            sha256=sha256,
            content_type=content_type,
            metadata=metadata,
            overwrite=overwrite,
        )

    def generate_presigned_put(
        self,
        key: str,
        *,
        bucket: str | None = None,
        expires_seconds: int = 3600,
        content_length: int,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        checksum_sha256: str = "",
    ) -> S3PresignedUpload:
        return S3PresignedUpload(
            url=f"memory://{bucket or 'default'}/{key}?expires={expires_seconds}",
            headers={
                "content-length": str(content_length),
                "content-type": content_type,
                **({"x-amz-checksum-sha256": checksum_sha256} if checksum_sha256 else {}),
                **{f"x-amz-meta-{name}": value for name, value in (metadata or {}).items()},
            },
        )

    def generate_presigned_put_url_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int,
        content_length: int,
        content_type: str,
    ) -> str:
        self.workspace_put_urls.append((workspace_id, bucket, key))
        return self.generate_presigned_put(
            f"workspaces/{workspace_id}/{bucket}/{key}",
            bucket=bucket,
            expires_seconds=expires_seconds,
            content_length=content_length,
            content_type=content_type,
        ).url

    def download_file(
        self,
        bucket: str,
        key: str,
        target: str | Path,
    ) -> ObjectRecord:
        payload = self.files[(bucket, key)]
        Path(target).write_bytes(payload)
        return _fake_object_record(bucket, key, payload, path=str(target))

    def download_file_for_workspace(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        target: str | Path,
    ) -> ObjectRecord:
        _ = workspace_id
        return self.download_file(bucket, key, target)


class _FakeCacheStorage:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    def put(self, namespace: str, key: str, source: str | Path) -> CacheEntry:
        payload = Path(source).read_bytes()
        path = f"{namespace}/{key}"
        self.files[(namespace, key)] = payload
        return CacheEntry(
            key=f"{namespace}:{key}",
            path=path,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )


def _fake_object_record(
    bucket: str,
    key: str,
    payload: bytes,
    *,
    path: str | None = None,
) -> ObjectRecord:
    return ObjectRecord(
        id=f"{bucket}/{key}",
        bucket=bucket,
        key=key,
        path=path or f"{bucket}/{key}",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_worker_container_routes_are_bound_to_the_container_the_worker_was_given(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    """A worker may bill for and act on a container placed on it, and no other.

    Both requests are worker-authored down to the workspace that pays, and a
    worker token lives on hardware a customer joined. Without this the cheapest
    attack on the platform is billing somebody else's workspace for GPU-seconds
    nobody ran, or ending somebody else's container by declaring it failed.
    """

    redis = real_redis_actors.client()
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "usage-owner")
    workspace_owner_user_id(isolated_services.context, workspace.id)
    for pool in ("pool-assigned", "pool-stranger"):
        isolated_services.compute.create_unit(
            UnitName(pool),
            workspace=workspace.id,
            worker_cpu_millicores=1000,
            worker_memory_mib=1024,
        )
    stub = control.create_stub("usage-owner", workspace=workspace.id)
    container_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="usage-owner",
                image="",
                command=[],
                workspace_id=workspace.id,
                stub_id=stub.id,
                runtime_worker_id="worker-assigned",
                runtime_machine_id="machine-worker-assigned",
            )
        )

    client = client_stack.enter_context(
        TestClient(create_app(_api_services(isolated_services, redis)))
    )
    sessions = {
        name: _register_worker_session(
            isolated_services,
            workspace.id,
            client,
            _worker_token(isolated_services, "usage-owner"),
            SchedulerWorkerRecord(
                capacity_owner_id="11111111-1111-4111-8111-111111111111",
                worker_id=name,
                machine_id=f"machine-{name}",
                pool=MachinePool(pool),
                status=SchedulerWorkerStatus.Available,
                total_cpu_millicores=1000,
                total_memory_mib=1024,
                free_cpu_millicores=1000,
                free_memory_mib=1024,
            ),
        )
        for name, pool in (
            ("worker-assigned", "pool-assigned"),
            ("worker-stranger", "pool-stranger"),
        )
    }

    def _record(worker: str) -> int:
        return client.post(
            "/worker-repository/record-worker-usage",
            json={
                "record": UsageRecord(
                    id=str(uuid4()),
                    workspace_id=workspace.id,
                    resource_type="container",
                    resource_id=container_id,
                    metric=UsageMetric.CpuUsedCoreSeconds,
                    unit=UsageUnit.Seconds,
                    quantity=3600.0,
                ).model_dump(mode="json")
            },
            headers=sessions[worker],
        ).status_code

    def _declare_failed(worker: str) -> int:
        return client.post(
            "/worker-repository/update-container-status",
            json={
                "container_id": container_id,
                "status": SchedulerContainerStatus.Failed.value,
            },
            headers=sessions[worker],
        ).status_code

    assert _record("worker-assigned") == 200
    assert _record("worker-stranger") == 403
    # Nothing holds scheduler state for this container, so the worker it was
    # placed on gets as far as finding none (404) and the stranger never does.
    assert _declare_failed("worker-stranger") == 403
    assert _declare_failed("worker-assigned") == 404


def test_worker_usage_is_bounded_by_the_container_lifetime_the_platform_recorded(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
    client_stack: ExitStack,
) -> None:
    """The ledger prices from the window, and the worker is not its only author.

    A window reaching outside the lifetime the control plane recorded is cut back
    to it, and one lying entirely outside is refused: otherwise a worker states
    the interval it is paid for, and every reader downstream — the split across a
    rate change, the period the charge lands in — believes it.
    """

    redis = real_redis_actors.client()
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "window-owner")
    workspace_owner_user_id(isolated_services.context, workspace.id)
    isolated_services.compute.create_unit(
        UnitName("pool-window"),
        workspace=workspace.id,
        worker_cpu_millicores=1000,
        worker_memory_mib=1024,
    )
    stub = control.create_stub("window-owner", workspace=workspace.id)
    started_at = utc_now() - timedelta(hours=1)
    finished_at = started_at + timedelta(minutes=30)
    container_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="window-owner",
                image="",
                command=[],
                workspace_id=workspace.id,
                stub_id=stub.id,
                runtime_worker_id="worker-window",
                runtime_machine_id="machine-worker-window",
                status=ContainerStatus.Exited,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    client = client_stack.enter_context(
        TestClient(create_app(_api_services(isolated_services, redis)))
    )
    headers = _register_worker_session(
        isolated_services,
        workspace.id,
        client,
        _worker_token(isolated_services, "window-owner"),
        SchedulerWorkerRecord(
            capacity_owner_id="22222222-2222-4222-8222-222222222222",
            worker_id="worker-window",
            machine_id="machine-worker-window",
            pool=MachinePool("pool-window"),
            status=SchedulerWorkerStatus.Available,
            total_cpu_millicores=1000,
            total_memory_mib=1024,
            free_cpu_millicores=1000,
            free_memory_mib=1024,
        ),
    )

    def _record(window_started_at: datetime, window_ended_at: datetime) -> tuple[int, bytes]:
        response = client.post(
            "/worker-repository/record-worker-usage",
            json={
                "record": UsageRecord(
                    id=str(uuid4()),
                    workspace_id=workspace.id,
                    resource_type="container",
                    resource_id=container_id,
                    metric=UsageMetric.CpuUsedCoreSeconds,
                    unit=UsageUnit.Seconds,
                    quantity=3600.0,
                    metadata={
                        METERING_WINDOW_STARTED_AT_METADATA_KEY: window_started_at.isoformat(),
                        METERING_WINDOW_ENDED_AT_METADATA_KEY: window_ended_at.isoformat(),
                    },
                ).model_dump(mode="json")
            },
            headers=headers,
        )
        return (response.status_code, response.content)

    claimed_start = started_at - timedelta(hours=6)
    claimed_end = finished_at + timedelta(hours=6)
    status_code, body = _record(claimed_start, claimed_end)
    assert status_code == 200
    accepted = RecordWorkerUsageResponse.model_validate_json(body).record
    assert accepted is not None
    window_start, window_end = (
        datetime.fromisoformat(str(accepted.metadata[key]))
        for key in (
            METERING_WINDOW_STARTED_AT_METADATA_KEY,
            METERING_WINDOW_ENDED_AT_METADATA_KEY,
        )
    )
    # The whole lifetime is kept and only a clock-skew margin beyond it: the
    # eleven hours the worker claimed either side are not billable.
    assert window_start <= started_at
    assert window_end >= finished_at
    assert window_start - claimed_start > timedelta(hours=5)
    assert claimed_end - window_end > timedelta(hours=5)

    refused, _ = _record(finished_at + timedelta(days=1), finished_at + timedelta(days=2))
    assert refused == 403


def test_worker_repository_exit_releases_what_a_pooled_container_had_claimed(
    isolated_services: ApiServices,
    real_redis_actors: RealRedisActors,
) -> None:
    """A crashed pooled container gives its invocations back to the pool.

    This is the common shape of an uncommanded death — OOM, a node lost, user
    code that segfaults — and the control plane never asked for it, so nothing
    on the stop path runs. A function container carries no task id, so the
    terminal-state sync that settles a container-addressed task cannot see what
    this one was running. Left unreleased the task keeps naming a dead
    container: no claim query can see it, no retry reaches it, and its caller
    waits forever.
    """

    redis = real_redis_actors.client()
    service = _worker_repository_service(isolated_services, redis)
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "pooled-exit",
        kind=StubKind.Function,
        handler="pkg.jobs:handler",
        config={"image": {"image_id": "image"}},
    )
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        container = ContainerRepository(session).records.create(
            {
                "name": "pooled-exit",
                "image": FUNCTION_IMAGE,
                "command": ["python", "-m", "runner.function"],
                "workspace_id": workspace_id,
                "stub_id": stub.id,
                "runtime_worker_id": "worker-1",
                "status": ContainerStatus.Running.value,
            },
            workspace_id=workspace_id,
            name="pooled-exit",
            status=ContainerStatus.Running.value,
        )
    claimed = isolated_services.tasks.create(
        "claimed-invocation",
        workspace_id=stub.workspace_id,
        stub_id=stub.id,
    )
    isolated_services.tasks.start(claimed.id, container_id=container.id)

    service.set_container_exit_code(
        SetContainerExitCodeRequest(container_id=container.id, exit_code=137),
        principal=WorkerRepositoryPrincipal(worker_id="worker-1"),
    )

    released = isolated_services.tasks.get(claimed.id)
    assert released.container_id is None, (
        f"task {released.id} still names the container that died holding it, so no "
        "claim can see it and its caller waits forever"
    )
    assert released.status is TaskStatus.Pending

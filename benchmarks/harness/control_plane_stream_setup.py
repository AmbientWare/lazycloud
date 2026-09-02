from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ParamSpec, TypeVar
from uuid import uuid4

from cli.api_client import AdminApiClient
from coordination.redis_client import RedisClient
from identity.auth import AuthService, IdentityDatabaseContext
from identity.token_invalidation import AuthTokenInvalidation
from lazycloud.clients.compute.control import ComputeClient
from lazycloud.clients.workspace.control import WorkspaceControlClient
from pydantic import SecretStr
from shared.compute_policy import MachinePool
from shared.containers import ContainerStatus
from shared.http.compute import ContainerDetailResponse, ContainerRunRequest, UnitCreateRequest
from shared.http.compute_policy import WorkspaceComputePolicyUpdateRequest
from shared.http.system import TokenCreateRequest
from shared.http_transport import HttpChannel
from shared.identity import AuthScope, TokenKind
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from worker.events import ContainerEventPayload
from worker.repository_client import WorkerRepositoryHttpClient, WorkerRepositoryHttpTransport
from worker.repository_payloads import (
    AddWorkerRequest,
    GetNextContainerRequestRequest,
    WorkerCacheSession,
    WorkerCacheSessionRequest,
    WorkerIdRequest,
)

from benchmarks.harness.control_plane_streams import (
    ControlPlaneStreamConfig,
    ControlPlaneStreamManifest,
    ControlPlaneStreamReport,
    CustomerStreamTarget,
    DeliveryWorkerStreamIdentity,
    DurableContainerExpectation,
    WorkerStreamIdentity,
    emit_progress,
    run_control_plane_stream_benchmark,
)
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings

DEFAULT_ASSIGNMENT_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_DELIVERY_IMAGE = "python:3.12-slim"

_P = ParamSpec("_P")
_T = TypeVar("_T")


class ControlPlaneStreamProvisioningError(RuntimeError):
    pass


@dataclass(slots=True)
class _ProvisionedWorker:
    worker_id: str
    bootstrap_token_id: str
    session_token: SecretStr
    cache_session: WorkerCacheSession
    client: WorkerRepositoryHttpClient = field(repr=False)


@dataclass(slots=True)
class _ProvisioningRecord:
    workspace_id: str = ""
    workspace_name: str = ""
    customer_token_id: str = ""
    customer_token: SecretStr = field(default_factory=lambda: SecretStr(""), repr=False)
    unit_ids: list[str] = field(default_factory=list)
    container_id: str = ""
    workers: list[_ProvisionedWorker] = field(default_factory=list, repr=False)
    manifest_path: Path | None = None
    manifest_directory: Path | None = None


class ControlPlaneStreamProvisioner:
    def __init__(
        self,
        *,
        endpoint: str,
        admin_token: str,
        run_id: str,
        assignment_timeout_seconds: float = DEFAULT_ASSIGNMENT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not admin_token:
            raise ControlPlaneStreamProvisioningError(
                "provisioning requires BENCHMARK_ADMIN_TOKEN or --admin-token"
            )
        self.endpoint = endpoint.rstrip("/")
        self.admin_token = SecretStr(admin_token)
        self.run_id = run_id
        self.assignment_timeout_seconds = assignment_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.record = _ProvisioningRecord()
        self.cleanup_errors: list[str] = []

    def prepare(self) -> Path:
        workspace_name = f"stream-bench-{self.run_id}"
        delivery_pool = MachinePool(f"stream-bench-{self.run_id}-delivery")
        load_pool = MachinePool(f"stream-bench-{self.run_id}-load")
        workspace = self._workspace_client().create(workspace_name)
        self.record.workspace_id = workspace.id
        self.record.workspace_name = workspace.name
        emit_progress("provision-workspace-created", workspace_id=workspace.id)

        admin = self._admin_client(
            token=self.admin_token.get_secret_value(),
            workspace=workspace.id,
        )
        customer = admin.create_token(TokenCreateRequest(name=f"stream-bench-{self.run_id}"))
        self.record.customer_token_id = customer.record.id
        self.record.customer_token = SecretStr(customer.token)

        customer_api = self._admin_client(token=customer.token, workspace=workspace.id)
        delivery_unit = customer_api.create_unit(
            UnitCreateRequest(
                name=f"stream-bench-{self.run_id}-delivery",
                pool=delivery_pool,
                provider="local",
                initial_machines=0,
                min_machines=0,
                max_machines=0,
                default_eligible=True,
                worker_cpu_millicores=4_000,
                worker_memory_mib=8_192,
                worker_runtimes=("runsc",),
            )
        )
        load_unit = customer_api.create_unit(
            UnitCreateRequest(
                name=f"stream-bench-{self.run_id}-load",
                pool=load_pool,
                provider="local",
                initial_machines=0,
                min_machines=0,
                max_machines=0,
                worker_runtimes=("runsc",),
            )
        )
        self.record.unit_ids.extend((delivery_unit.id, load_unit.id))
        compute = ComputeClient.from_endpoint(
            self.endpoint,
            token=customer.token,
            timeout_seconds=self.request_timeout_seconds,
            workspace=workspace.id,
        )
        policy = compute.policy()
        compute.update_policy(
            WorkspaceComputePolicyUpdateRequest(
                expected_revision=policy.revision,
                default_pool=str(delivery_pool),
            )
        )
        emit_progress(
            "provision-pools-created",
            delivery_pool=str(delivery_pool),
            load_pool=str(load_pool),
        )

        load = self._register_worker(
            role="load",
            pool=load_pool,
            capacity_owner_id=load_unit.capacity_owner_id,
            cpu_millicores=0,
            memory_mib=0,
        )
        delivery = self._register_worker(
            role="delivery",
            pool=delivery_pool,
            capacity_owner_id=delivery_unit.capacity_owner_id,
            cpu_millicores=4_000,
            memory_mib=8_192,
        )

        container = customer_api.run_container(
            ContainerRunRequest(
                name=f"stream-bench-{self.run_id}",
                image=DEFAULT_DELIVERY_IMAGE,
                command=["sleep", "600"],
            )
        )
        self.record.container_id = container.id
        assigned = self._await_delivery_assignment(
            customer_api,
            delivery_worker_id=delivery.worker_id,
            load_worker_id=load.worker_id,
        )
        manifest = ControlPlaneStreamManifest(
            customer=CustomerStreamTarget(
                token=SecretStr(customer.token),
                workspace=workspace.id,
                container_id=container.id,
                event_payload=ContainerEventPayload(
                    id=f"benchmark-{self.run_id}",
                    container_id=container.id,
                    workspace_id=workspace.id,
                    worker_id=delivery.worker_id,
                    source="control-plane-stream-benchmark",
                    attrs={"benchmark_run_id": self.run_id},
                ),
            ),
            load_worker=WorkerStreamIdentity(
                token=load.session_token,
                request=self._worker_request(load),
            ),
            delivery_worker=DeliveryWorkerStreamIdentity(
                token=delivery.session_token,
                request=self._worker_request(delivery),
                expected_requests=(
                    DurableContainerExpectation(
                        container_id=assigned.id,
                        workspace=workspace.id,
                        allowed_statuses=(ContainerStatus.Pending,),
                    ),
                ),
            ),
        )
        return self._write_manifest(manifest)

    def cleanup(self) -> tuple[str, ...]:
        record = self.record
        customer_token = record.customer_token.get_secret_value()
        if record.container_id and customer_token and record.workspace_id:
            customer = self._admin_client(token=customer_token, workspace=record.workspace_id)
            self._cleanup_call("stop container", customer.stop_container, record.container_id)
            self._cleanup_call("delete container", customer.delete_container, record.container_id)

        for worker in reversed(record.workers):
            self._cleanup_call(
                f"remove worker {worker.worker_id}",
                worker.client.remove_worker,
                WorkerIdRequest(worker_id=worker.worker_id),
            )

        if record.unit_ids and customer_token and record.workspace_id:
            customer = self._admin_client(token=customer_token, workspace=record.workspace_id)
            for unit_id in reversed(record.unit_ids):
                self._cleanup_call(f"delete unit {unit_id}", customer.delete_unit, unit_id)

        for worker in record.workers:
            self._cleanup_call(
                f"revoke bootstrap token for {worker.worker_id}",
                self._revoke_service_token,
                worker.bootstrap_token_id,
            )

        if record.customer_token_id:
            admin = self._admin_client(token=self.admin_token.get_secret_value(), workspace="")
            self._cleanup_call(
                "revoke customer token", admin.revoke_token, record.customer_token_id
            )

        if record.workspace_name:
            self._cleanup_call(
                "delete workspace", self._workspace_client().delete, record.workspace_name
            )

        if record.manifest_path is not None:
            self._cleanup_call(
                "delete secret manifest", record.manifest_path.unlink, missing_ok=True
            )
        if record.manifest_directory is not None:
            self._cleanup_call("delete manifest directory", record.manifest_directory.rmdir)
        return tuple(self.cleanup_errors)

    def _register_worker(
        self,
        *,
        role: str,
        pool: MachinePool,
        capacity_owner_id: str,
        cpu_millicores: int,
        memory_mib: int,
    ) -> _ProvisionedWorker:
        worker_id = f"benchmark-{self.run_id}-{role}"
        bootstrap_name = f"stream-bench-{self.run_id}-{role}"
        bootstrap_token, bootstrap_token_id = self._create_service_token(bootstrap_name)
        transport = WorkerRepositoryHttpTransport(
            endpoint=self.endpoint,
            token=bootstrap_token,
            timeout_seconds=self.request_timeout_seconds,
        )
        client = WorkerRepositoryHttpClient(transport)
        generation_id = uuid4().hex
        response = client.add_worker(
            AddWorkerRequest(
                worker=SchedulerWorkerRecord(
                    worker_id=worker_id,
                    pool=pool,
                    capacity_owner_id=capacity_owner_id,
                    status=SchedulerWorkerStatus.Pending,
                    runtime_class="runsc",
                    runtime_classes=["runsc"],
                    free_cpu_millicores=cpu_millicores,
                    free_memory_mib=memory_mib,
                    total_cpu_millicores=cpu_millicores,
                    total_memory_mib=memory_mib,
                ),
                cache_generation_id=generation_id,
                cache_storage_id=f"node:{worker_id}",
            )
        )
        if not response.worker_session_token or response.cache_session is None:
            raise ControlPlaneStreamProvisioningError(
                f"worker registration did not return a bound session for {worker_id}"
            )
        worker = _ProvisionedWorker(
            worker_id=worker_id,
            bootstrap_token_id=bootstrap_token_id,
            session_token=SecretStr(response.worker_session_token),
            cache_session=response.cache_session,
            client=client,
        )
        self.record.workers.append(worker)
        active = client.activate_source_cache(
            WorkerCacheSessionRequest(
                worker_id=worker_id,
                cache_generation_id=response.cache_session.generation_id,
                cache_session_fence=response.cache_session.session_fence,
            )
        )
        if active.worker is None or active.worker.status is not SchedulerWorkerStatus.Available:
            raise ControlPlaneStreamProvisioningError(
                f"worker did not become available: {worker_id}"
            )
        emit_progress("provision-worker-ready", worker_id=worker_id, role=role)
        return worker

    # No HTTP route mints a worker bootstrap token. The worker-bootstrap
    # process writes one through the identity service, and this opens the
    # same owners it does.
    @staticmethod
    @contextmanager
    def _auth_service() -> Iterator[AuthService]:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.WorkerBootstrap)
        )
        redis = RedisClient.from_settings()
        try:
            yield AuthService(
                IdentityDatabaseContext(database),
                token_invalidation=AuthTokenInvalidation.from_redis(redis),
            )
        finally:
            redis.close()
            database.dispose()

    def _create_service_token(self, name: str) -> tuple[str, str]:
        with self._auth_service() as auth:
            raw, record = auth.create_service_token(
                name,
                kind=TokenKind.Worker,
                workspace_id=self.record.workspace_id,
                scopes=[AuthScope.Worker.value],
            )
            return raw, record.id

    def _revoke_service_token(self, token_id: str) -> None:
        with self._auth_service() as auth:
            auth.revoke_token(token_id)

    def _await_delivery_assignment(
        self,
        customer: AdminApiClient,
        *,
        delivery_worker_id: str,
        load_worker_id: str,
    ) -> ContainerDetailResponse:
        deadline = time.monotonic() + self.assignment_timeout_seconds
        while True:
            container = customer.get_container(self.record.container_id)
            workers = {
                worker.id: worker.status
                for worker in customer.list_workers().workers
                if worker.id in {load_worker_id, delivery_worker_id}
            }
            emit_progress(
                "provision-delivery-assignment",
                container_id=container.id,
                runtime_worker_id=container.runtime_worker_id,
                worker_statuses=workers,
            )
            if container.runtime_worker_id == delivery_worker_id:
                return container
            if container.runtime_worker_id:
                raise ControlPlaneStreamProvisioningError(
                    "benchmark request was assigned outside its delivery worker"
                )
            if time.monotonic() >= deadline:
                raise ControlPlaneStreamProvisioningError(
                    "benchmark delivery request was not assigned before the deadline"
                )
            time.sleep(0.25)

    def _write_manifest(self, manifest: ControlPlaneStreamManifest) -> Path:
        directory = Path(tempfile.mkdtemp(prefix=f"lazycloud-stream-{self.run_id}-"))
        directory.chmod(0o700)
        path = directory / "manifest.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            manifest_data = manifest.model_dump(mode="json")
            manifest_data["customer"]["token"] = manifest.customer.token.get_secret_value()
            manifest_data["load_worker"]["token"] = manifest.load_worker.token.get_secret_value()
            manifest_data["delivery_worker"]["token"] = (
                manifest.delivery_worker.token.get_secret_value()
            )
            payload = (json.dumps(manifest_data, indent=2) + "\n").encode()
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        if path.stat().st_mode & 0o777 != 0o600:
            raise ControlPlaneStreamProvisioningError("secret manifest mode is not 0600")
        self.record.manifest_directory = directory
        self.record.manifest_path = path
        emit_progress("provision-manifest-ready")
        return path

    @staticmethod
    def _worker_request(worker: _ProvisionedWorker) -> GetNextContainerRequestRequest:
        return GetNextContainerRequestRequest(
            worker_id=worker.worker_id,
            cache_generation_id=worker.cache_session.generation_id,
            cache_session_fence=worker.cache_session.session_fence,
        )

    def _workspace_client(self) -> WorkspaceControlClient:
        return WorkspaceControlClient.from_endpoint(
            self.endpoint,
            token=self.admin_token.get_secret_value(),
            timeout_seconds=self.request_timeout_seconds,
        )

    def _admin_client(self, *, token: str, workspace: str) -> AdminApiClient:
        return AdminApiClient(
            channel=HttpChannel(
                endpoint=self.endpoint,
                token=token,
                timeout_seconds=self.request_timeout_seconds,
            ),
            workspace=workspace,
        )

    def _cleanup_call(
        self,
        operation: str,
        function: Callable[_P, _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> None:
        try:
            function(*args, **kwargs)
        except Exception as exc:
            self.cleanup_errors.append(f"{operation}: {type(exc).__name__}")


def run_provisioned_control_plane_stream_benchmark(
    config: ControlPlaneStreamConfig,
    *,
    admin_token: str,
) -> ControlPlaneStreamReport:
    provisioner = ControlPlaneStreamProvisioner(
        endpoint=config.endpoint,
        admin_token=admin_token,
        run_id=config.run_id,
    )
    try:
        manifest = provisioner.prepare()
        report = run_control_plane_stream_benchmark(replace(config, manifest=manifest))
    except BaseException as exc:
        cleanup_errors = provisioner.cleanup()
        if not isinstance(exc, Exception):
            raise
        detail = f"; cleanup errors: {len(cleanup_errors)}" if cleanup_errors else ""
        raise ControlPlaneStreamProvisioningError(
            f"control-plane stream provisioning failed with {type(exc).__name__}{detail}"
        ) from exc
    cleanup_errors = provisioner.cleanup()
    if cleanup_errors:
        return report.model_copy(
            update={
                "failure": f"benchmark cleanup failed in {len(cleanup_errors)} operation(s)",
                "passed": False,
            }
        )
    return report


__all__ = [
    "ControlPlaneStreamProvisioningError",
    "run_provisioned_control_plane_stream_benchmark",
]

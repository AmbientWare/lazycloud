from __future__ import annotations

import logging
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import Field, field_validator
from shared.app_identity import WORKER_BUNDLE_ROOT
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.routing import (
    AgentBackendRoute,
    BackendRouteKind,
    BackendRouteTransport,
)
from shared.scheduling import (
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
)
from shared.worker_events import WorkerEventRecord

from worker.container_execution import (
    ContainerExecutionContext,
    ContainerWorkspaceStorageMounter,
)
from worker.container_rootfs import ContainerRootfsReleaser
from worker.container_service.models import WorkerContainerServiceInstance
from worker.container_service.protocols import (
    LocalSandboxPortPublisher,
    WorkerContainerInstanceStore,
    WorkerContainerRuntimeController,
    WorkerSandboxDockerLifecycle,
)
from worker.events import ContainerEventPayload, ContainerRequestContext, WorkerBuildCancelRegistry
from worker.execution import (
    ContainerNetworkIdentity,
    NetworkAddressMode,
    PortBinding,
    container_port_address_map,
    select_container_network,
)
from worker.request_mounts import WorkerRequestMountCleaner
from worker.routes import (
    WorkerPortBinding,
    WorkerRouteContext,
    build_agent_backend_route,
    plan_container_route_registration,
)
from worker.runtime_config import RuntimeContainerStatus
from worker.source_code import SourceWorkspaceLifecycle

LOGGER = logging.getLogger(__name__)

DEFAULT_WORKER_UPLOAD_ROOT = "/tmp/container-uploads"
FORCE_KILL_SIGNAL = 9
GRACEFUL_STOP_SIGNAL = 15
DEFAULT_GRACEFUL_STOP_TIMEOUT_SECONDS = 5.0
DEFAULT_GRACEFUL_STOP_POLL_SECONDS = 0.1


@runtime_checkable
class WorkerContainerStopReasonRecorder(Protocol):
    def record_stop_reason(
        self,
        container_id: str,
        reason: StopContainerReason,
    ) -> None: ...


class ContainerIpResolver(Protocol):
    def container_ip(self, container_id: str) -> str: ...


class WorkerOomWatcherStopper(Protocol):
    def stop_oom_watcher(self, container_id: str) -> None: ...


class WorkerGpuReleaser(Protocol):
    def release_gpu(self, container_id: str) -> None: ...


class WorkerCheckpointSignalCleaner(Protocol):
    def cleanup(self, container_id: str) -> None: ...


class WorkerNetworkTeardown(Protocol):
    def teardown_network(self, container_id: str) -> None: ...


class WorkerNetworkPortExposer(Protocol):
    def expose_port(self, container_id: str, binding: PortBinding) -> None: ...

    def unexpose_port(self, container_id: str, binding: PortBinding) -> None: ...


class WorkerHostPortAllocator(Protocol):
    def allocate_ports(self, count: int) -> list[int]: ...


@runtime_checkable
class WorkerContainerInstanceDeleter(Protocol):
    def delete_container_instance(self, container_id: str) -> bool: ...


class WorkerEventAppendSink(Protocol):
    def append(self, record: WorkerEventRecord) -> WorkerEventRecord: ...


class SchedulerContainerRouteRepository(Protocol):
    def set_worker_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress: ...

    def set_container_address(
        self,
        container_id: str,
        address: str,
        *,
        route: AgentBackendRoute | None = None,
    ) -> SchedulerContainerAddress: ...

    def set_container_address_map(
        self,
        container_id: str,
        address_map: dict[int, str],
        *,
        routes: list[AgentBackendRoute] | None = None,
    ) -> SchedulerContainerAddressMap: ...

    def get_container_address_map(self, container_id: str) -> SchedulerContainerAddressMap: ...


class WorkerRouteIdentity(ContractModel):
    worker_id: str
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    machine_id: str = ""
    pod_address: str = ""
    container_service_port: int = 0
    persistent: bool = False
    route_transport: BackendRouteTransport = BackendRouteTransport.PrivateNetwork
    route_local_target_host: str = ""
    agent_worker: bool = True

    @field_validator("container_service_port")
    @classmethod
    def container_service_port_must_be_valid(cls, value: int) -> int:
        if value < 0 or value > 65535:
            msg = "container service port must be between 0 and 65535"
            raise ValueError(msg)
        return value

    @property
    def worker_address(self) -> str:
        if self.container_service_port > 0 and self.pod_address:
            return f"{self.pod_address}:{self.container_service_port}"
        return self.pod_address

    def route_context(self, request: ContainerRequestContext) -> WorkerRouteContext | None:
        if not (request.workspace_id and request.container_id):
            return None
        if self.agent_worker and not (self.pool and self.machine_id and self.worker_id):
            return None
        return WorkerRouteContext(
            workspace_id=request.workspace_id,
            pool=self.pool,
            machine_id=self.machine_id,
            worker_id=self.worker_id,
            container_id=request.container_id,
            transport=self.route_transport,
            local_target_host=self.route_local_target_host,
        )


class WorkerRoutePublicationResult(ContractModel):
    container_id: str
    worker_address: str = ""
    primary_target: str = ""
    address_map: dict[int, str] = Field(default_factory=dict)
    routes: list[AgentBackendRoute] = Field(default_factory=list)


@dataclass(slots=True)
class SchedulerWorkerAddressPublisher:
    identity: WorkerRouteIdentity
    containers: SchedulerContainerRouteRepository

    def publish_worker_address(
        self,
        request: ContainerRequestContext,
    ) -> WorkerRoutePublicationResult:
        address = self.identity.worker_address
        if not address:
            msg = "worker address is required"
            raise ValueError(msg)

        route = self._worker_route(request, address)
        scheduler_route = route
        self.containers.set_worker_address(
            request.container_id,
            address,
            route=scheduler_route,
        )
        return WorkerRoutePublicationResult(
            container_id=request.container_id,
            worker_address=address,
            routes=[scheduler_route] if scheduler_route is not None else [],
        )

    def _worker_route(
        self,
        request: ContainerRequestContext,
        address: str,
    ) -> AgentBackendRoute | None:
        route_context = self.identity.route_context(request)
        if route_context is None:
            return None
        return build_agent_backend_route(
            route_context,
            kind=BackendRouteKind.Worker,
            port=0,
            local_target=address,
            agent_worker=self.identity.agent_worker,
        )


@dataclass(slots=True)
class SchedulerContainerRoutePublisher:
    identity: WorkerRouteIdentity
    containers: SchedulerContainerRouteRepository
    container_ips: ContainerIpResolver | None = None

    def publish_container_routes(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> WorkerRoutePublicationResult:
        route_context = self.identity.route_context(context.request)
        if route_context is None:
            msg = "worker route context is incomplete"
            raise ValueError(msg)
        address_map = self._address_map(context.request, port_bindings)
        bindings = [
            WorkerPortBinding(container_port=binding.container_port) for binding in port_bindings
        ]
        plan = plan_container_route_registration(
            route_context,
            bindings=bindings,
            address_map=address_map,
            agent_worker=self.identity.agent_worker,
        )
        if not plan.ok:
            raise ValueError(plan.error_message)

        routes = [
            scheduler_route
            for scheduler_route in (route for route in plan.routes)
            if scheduler_route is not None
        ]
        primary_route = next(
            (route for route in routes if route.port == plan.primary_port),
            None,
        )
        if plan.primary_target:
            self.containers.set_container_address(
                context.request.container_id,
                plan.primary_target,
                route=primary_route,
            )
        self.containers.set_container_address_map(
            context.request.container_id,
            plan.address_map,
            routes=routes,
        )
        return WorkerRoutePublicationResult(
            container_id=context.request.container_id,
            primary_target=plan.primary_target,
            address_map=plan.address_map,
            routes=routes,
        )

    def _address_map(
        self,
        request: ContainerRequestContext,
        bindings: list[PortBinding],
    ) -> dict[int, str]:
        container_ip = (
            self.container_ips.container_ip(request.container_id) if self.container_ips else ""
        )
        selection = select_container_network(
            request.container_id,
            pod_address=self.identity.pod_address,
            persistent=self.identity.persistent,
            machine_id=self.identity.machine_id,
            transport=self.identity.route_transport.value,
            container_ip=container_ip,
        )
        identity = ContainerNetworkIdentity(
            container_id=request.container_id,
            pod_address=selection.identity.pod_address,
            container_ip=selection.identity.container_ip,
            mode=NetworkAddressMode(selection.identity.mode),
        )
        return container_port_address_map(identity, bindings).addresses


@dataclass(slots=True)
class SchedulerSandboxPortPublisher:
    identity: WorkerRouteIdentity
    containers: SchedulerContainerRouteRepository
    port_allocator: WorkerHostPortAllocator | None = None
    network: WorkerNetworkPortExposer | None = None
    fallback: LocalSandboxPortPublisher = field(default_factory=LocalSandboxPortPublisher)

    def allocate_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        container_port: int,
    ) -> int:
        if instance.route_transport is BackendRouteTransport.Direct:
            if self.port_allocator is None:
                msg = "host port allocator is required for direct sandbox port exposure"
                raise RuntimeError(msg)
            allocated = self.port_allocator.allocate_ports(1)
            if len(allocated) != 1:
                msg = "host port allocator did not return one port"
                raise RuntimeError(msg)
            return allocated[0]
        if instance.container_ip:
            return container_port
        return self.fallback.allocate_port(instance, container_port=container_port)

    def local_target(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        host_port: int,
        container_port: int,
    ) -> str:
        if instance.route_transport is BackendRouteTransport.Direct:
            host = instance.route_local_target_host or self.identity.pod_address
            if not host:
                msg = "direct sandbox route host is required"
                raise RuntimeError(msg)
            return f"{host}:{host_port}"
        if instance.container_ip:
            return f"{instance.container_ip}:{container_port}"
        return self.fallback.local_target(
            instance,
            host_port=host_port,
            container_port=container_port,
        )

    def publish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        binding: PortBinding | None = None,
        address_map: dict[int, str] | None = None,
        routes: list[AgentBackendRoute] | None = None,
    ) -> str:
        if binding is not None and instance.route_transport is BackendRouteTransport.Direct:
            if self.network is None:
                msg = "network port exposer is required for direct sandbox port exposure"
                raise RuntimeError(msg)
            self.network.expose_port(instance.container_id, binding)
        if address_map is not None:
            scheduler_routes = [
                scheduler_route
                for scheduler_route in (route for route in routes or [])
                if scheduler_route is not None
            ]
            updated_ports = {route.port for route in scheduler_routes}
            existing_routes = self.containers.get_container_address_map(
                instance.container_id
            ).routes
            merged_routes = [route for route in existing_routes if route.port not in updated_ports]
            merged_routes.extend(scheduler_routes)
            self.containers.set_container_address_map(
                instance.container_id,
                address_map,
                routes=merged_routes,
            )
        return self.fallback.publish_exposed_port(
            instance,
            port=port,
            local_target=local_target,
            binding=binding,
            address_map=address_map,
            routes=routes,
        )

    def unpublish_exposed_port(
        self,
        instance: WorkerContainerServiceInstance,
        *,
        port: int,
        local_target: str,
        address_map: dict[int, str],
    ) -> None:
        if instance.route_transport is BackendRouteTransport.Direct:
            if self.network is None:
                msg = "network port exposer is required for direct sandbox port removal"
                raise RuntimeError(msg)
            try:
                host_port = int(local_target.rsplit(":", maxsplit=1)[1])
            except (IndexError, ValueError) as exc:
                msg = f"invalid direct sandbox target: {local_target}"
                raise RuntimeError(msg) from exc
            self.network.unexpose_port(
                instance.container_id,
                PortBinding(host_port=host_port, container_port=port),
            )
        existing_routes = self.containers.get_container_address_map(instance.container_id).routes
        self.containers.set_container_address_map(
            instance.container_id,
            address_map,
            routes=[route for route in existing_routes if route.port != port],
        )
        self.fallback.unpublish_exposed_port(
            instance,
            port=port,
            local_target=local_target,
            address_map=address_map,
        )


@dataclass(slots=True)
class WorkerFinalizationCleanup:
    runtime: WorkerContainerRuntimeController | None = None
    instances: WorkerContainerInstanceStore | None = None
    gpu: WorkerGpuReleaser | None = None
    network: WorkerNetworkTeardown | None = None
    oom_watchers: WorkerOomWatcherStopper | None = None
    request_mounts: WorkerRequestMountCleaner | None = None
    checkpoint_signals: WorkerCheckpointSignalCleaner | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    source_workspaces: SourceWorkspaceLifecycle | None = None
    workspace_storage: ContainerWorkspaceStorageMounter | None = None
    container_rootfs: ContainerRootfsReleaser | None = None
    upload_root: Path = Path(DEFAULT_WORKER_UPLOAD_ROOT)
    bundle_root: Path = Path(WORKER_BUNDLE_ROOT)

    def release_gpu(self, container_id: str) -> None:
        if self.gpu is not None:
            self.gpu.release_gpu(container_id)

    def teardown_network(self, container_id: str) -> None:
        if self.network is not None:
            self.network.teardown_network(container_id)

    def remove_uploads(self, container_id: str) -> None:
        shutil.rmtree(self.upload_root / container_id, ignore_errors=True)

    def remove_source_workspace(self, container_id: str) -> None:
        if self.source_workspaces is not None:
            self.source_workspaces.cleanup_container(container_id)

    def force_stop_if_running(self, container_id: str) -> None:
        if self.sandbox_docker is not None:
            self.sandbox_docker.stop(container_id)
        if self.runtime is None:
            return
        live = _runtime_status_is_live(self.runtime.status(container_id))
        if not live:
            return
        self.runtime.kill_container(
            container_id,
            signal=FORCE_KILL_SIGNAL,
            force_delete=True,
        )
        if _runtime_status_is_live(self.runtime.status(container_id)):
            raise RuntimeError(f"container {container_id} is still running after force stop")

    def stop_oom_watcher(self, container_id: str) -> None:
        if self.oom_watchers is not None:
            self.oom_watchers.stop_oom_watcher(container_id)

    def unmount_request_mounts(self, container_id: str) -> None:
        if self.request_mounts is not None:
            self.request_mounts.unmount_request_mounts(container_id)

    def release_container_rootfs(self, container_id: str) -> None:
        if self.container_rootfs is None:
            return
        result = self.container_rootfs.release(container_id)
        if result.reason:
            # A failed unmount is reported, never swallowed: leaving the overlay
            # mounted strands the upper layer and its disk on this worker.
            raise RuntimeError(result.reason)

    def delete_local_state(self, container_id: str) -> None:
        if (
            not container_id
            or container_id in {".", ".."}
            or Path(container_id).name != container_id
        ):
            raise ValueError("container cleanup requires an owned path segment")
        instance = (
            self.instances.get_container_instance(container_id)
            if self.instances is not None
            else None
        )
        if self.checkpoint_signals is not None:
            self.checkpoint_signals.cleanup(container_id)
        bundle_path = self.bundle_root / container_id
        if (
            instance is not None
            and instance.bundle_path
            and Path(instance.bundle_path) != bundle_path
        ):
            raise RuntimeError(f"container bundle path does not belong to {container_id}")
        if bundle_path.is_symlink():
            bundle_path.unlink()
        elif bundle_path.exists():
            shutil.rmtree(bundle_path)
        if self.workspace_storage is None or self.instances is None:
            if isinstance(self.instances, WorkerContainerInstanceDeleter):
                self.instances.delete_container_instance(container_id)
            return
        active_workspace_names = {
            active.workspace_name
            for active in self.instances.list_container_instances()
            if active.workspace_name and active.container_id != container_id
        }
        results = self.workspace_storage.cleanup_unused(
            active_workspace_names=active_workspace_names,
        )
        failures = [
            result.output or result.reason or f"failed to unmount {result.local_path}"
            for result in results
            if not result.ok
        ]
        if failures:
            raise RuntimeError("; ".join(failures))
        if isinstance(self.instances, WorkerContainerInstanceDeleter):
            self.instances.delete_container_instance(container_id)


@dataclass(slots=True)
class WorkerRuntimeContainerStopper:
    runtime: WorkerContainerRuntimeController
    build_cancels: WorkerBuildCancelRegistry | None = None
    instances: WorkerContainerInstanceStore | None = None
    sandbox_docker: WorkerSandboxDockerLifecycle | None = None
    worker_id: str = ""
    graceful_timeout_seconds: float = DEFAULT_GRACEFUL_STOP_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_GRACEFUL_STOP_POLL_SECONDS

    def stop_container(
        self,
        container_id: str,
        *,
        force: bool,
        reason: StopContainerReason = StopContainerReason.Unknown,
    ) -> None:
        if self.build_cancels is not None and self.build_cancels.cancel(container_id).invoked:
            return
        if self.instances is not None:
            instance = self.instances.get_container_instance(container_id)
            if instance is None:
                # Nothing is assigned here, so this container is already not
                # running and the stop is satisfied. Raising instead would fail
                # the acknowledgement the control plane waits on, and a delete
                # that got exactly what it asked for would report that workers
                # never confirmed it. Pooled containers reach this constantly:
                # they exit on their own keep-warm window, so a stop routinely
                # arrives just after the container it names has gone.
                return
            if self.worker_id and instance.worker_id != self.worker_id:
                raise RuntimeError(
                    f"container {container_id} is assigned to worker {instance.worker_id!r}, "
                    f"not {self.worker_id!r}"
                )
        if self.sandbox_docker is not None:
            with suppress(Exception):
                self.sandbox_docker.stop(container_id)
        if isinstance(self.runtime, WorkerContainerStopReasonRecorder):
            self.runtime.record_stop_reason(container_id, reason)
        elif reason is not StopContainerReason.Unknown:
            raise RuntimeError("container runtime cannot persist the requested stop reason")
        if force:
            self.runtime.kill_container(
                container_id,
                signal=FORCE_KILL_SIGNAL,
                force_delete=True,
            )
            return
        self.runtime.kill_container(
            container_id,
            signal=GRACEFUL_STOP_SIGNAL,
            force_delete=False,
        )
        deadline = time.monotonic() + max(self.graceful_timeout_seconds, 0)
        while time.monotonic() < deadline:
            if not _runtime_status_is_live(self.runtime.status(container_id)):
                return
            time.sleep(max(self.poll_interval_seconds, 0))
        if _runtime_status_is_live(self.runtime.status(container_id)):
            self.runtime.kill_container(
                container_id,
                signal=FORCE_KILL_SIGNAL,
                force_delete=True,
            )


@dataclass(slots=True)
class WorkerContainerEventPublisher:
    sink: WorkerEventAppendSink
    worker_id: str

    def publish_container_exit(self, payload: ContainerEventPayload) -> WorkerEventRecord:
        return self.sink.append(
            WorkerEventRecord(
                id=str(uuid4()),
                worker_id=self.worker_id,
                event_type="container.exited",
                resource_id=payload.container_id,
                payload=payload.model_dump(mode="json"),
            )
        )


def _runtime_status_is_live(status: str) -> bool:
    return status in {
        RuntimeContainerStatus.Creating.value,
        RuntimeContainerStatus.Created.value,
        RuntimeContainerStatus.Running.value,
        RuntimeContainerStatus.Paused.value,
    }

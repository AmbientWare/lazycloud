from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from shared.container_requests import WORKER_USER_CODE_VOLUME
from worker.adapters import WorkerRouteIdentity
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerMountSetupResult,
    ContainerNetworkSetupResult,
)
from worker.container_service.models import (
    ContainerFilesystemSnapshotContext,
    SandboxDockerDaemonStatus,
    WorkerContainerServiceInstance,
)
from worker.container_service.protocols import WorkerContainerInstanceStore
from worker.execution import (
    ContainerNetworkIdentity,
    PortBinding,
    container_port_address_map,
)
from worker.oci_spec import OciRuntimeContainerSpec
from worker.runtime_config import OciRuntimeName
from worker.sandbox_server import SandboxContainerMount

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(slots=True)
class ContainerInstanceRuntimeResolver:
    """Answers "which runtime is this container on?" from the local instance store.

    The store is attached after the runtime controller is built, so the runtime
    of an unknown container is `None` rather than an error.
    """

    instances: WorkerContainerInstanceStore | None = None

    def __call__(self, container_id: str) -> OciRuntimeName | None:
        if self.instances is None:
            return None
        instance = self.instances.get_container_instance(container_id)
        return instance.runtime if instance is not None else None


@dataclass(slots=True)
class OciContainerServiceInstanceRecorder:
    instances: WorkerContainerInstanceStore
    identity: WorkerRouteIdentity
    cache_available: bool = False
    workspace_storage_available: bool = False

    def record_container_instance(
        self,
        context: ContainerExecutionContext,
        *,
        spec: OciRuntimeContainerSpec,
        mount_result: ContainerMountSetupResult,
        network_result: ContainerNetworkSetupResult | None,
        port_bindings: list[PortBinding],
    ) -> None:
        root_path = _spec_root_path(spec)
        identity = self.identity
        source_mounts = {
            (item.mount.mount_path, item.mount.local_path)
            for item in mount_result.mounts
            if item.included
            and item.mount.source_object_id
            and item.mount.mount_path == WORKER_USER_CODE_VOLUME
        }
        oci_mounts = spec.spec.get("mounts")
        if not isinstance(oci_mounts, list):
            raise ValueError("container OCI mount list is missing")
        excluded_paths: list[str] = []
        for mount in oci_mounts:
            if not isinstance(mount, dict):
                raise ValueError("container OCI mount destination is missing")
            destination, source = mount.get("destination"), mount.get("source")
            if not isinstance(destination, str) or not isinstance(source, str):
                raise ValueError("container OCI mount source or destination is missing")
            if (destination, source) not in source_mounts:
                excluded_paths.append(destination)
        instance = WorkerContainerServiceInstance(
            container_id=context.request.container_id,
            root_path=root_path,
            bundle_path=spec.bundle_path,
            config_path=spec.config_path,
            top_layer_path=root_path,
            filesystem_snapshot=ContainerFilesystemSnapshotContext(
                image_config=spec.image_config,
                architecture=context.architecture,
                excluded_paths=excluded_paths,
            ),
            workspace_path=str(Path(root_path) / "workspace"),
            cwd=context.cwd,
            runtime=context.runtime,
            env=_spec_process_env(spec),
            request_env=list(context.request.env),
            docker_enabled=context.docker_enabled,
            docker_daemon_status=(
                SandboxDockerDaemonStatus.Stopped
                if context.docker_enabled
                else SandboxDockerDaemonStatus.Disabled
            ),
            sandbox_supervisor_token_path=spec.sandbox_supervisor_token_path,
            ports=list(context.ports),
            exposed_ports=[binding.container_port for binding in port_bindings],
            address_map=_address_map(
                context.request.container_id,
                network_result,
                port_bindings,
            ),
            mounts=[
                SandboxContainerMount(
                    source=item.mount.local_path,
                    destination=item.mount.mount_path,
                )
                for item in mount_result.mounts
                if item.included and item.mount.local_path
            ],
            workspace_id=context.request.workspace_id,
            workspace_name=context.request.workspace_name,
            app_id=context.request.app_id,
            stub_id=context.request.stub_id,
            stub_type=context.request.stub_type,
            worker_id=identity.worker_id,
            machine_id=identity.machine_id,
            pool=identity.pool,
            image_id=context.request.image_id,
            container_ip=(
                network_result.identity.container_ip
                if network_result is not None and network_result.identity is not None
                else ""
            ),
            workspace_storage_available=(
                self.workspace_storage_available or context.request.workspace_storage_available
            ),
            cache_available=self.cache_available,
            gpu=context.request.gpu,
            gpu_count=context.request.gpu_count,
        )
        self.instances.save_container_instance(instance)


def _address_map(
    container_id: str,
    network_result: ContainerNetworkSetupResult | None,
    port_bindings: list[PortBinding],
) -> dict[int, str]:
    container_ip = (
        network_result.identity.container_ip
        if network_result is not None and network_result.identity is not None
        else ""
    )
    identity = ContainerNetworkIdentity(container_id=container_id, container_ip=container_ip)
    return container_port_address_map(identity, port_bindings).addresses


def _spec_root_path(spec: OciRuntimeContainerSpec) -> str:
    root = _oci_spec_document(spec).get("root")
    if isinstance(root, dict):
        path = root.get("path")
        if isinstance(path, str) and path:
            return path
    return str(Path(spec.bundle_path) / "rootfs")


def _spec_process_env(spec: OciRuntimeContainerSpec) -> list[str]:
    process = _oci_spec_document(spec).get("process")
    if not isinstance(process, dict):
        return []
    env = process.get("env")
    if not isinstance(env, list):
        return []
    return [item for item in env if isinstance(item, str)]


def _oci_spec_document(spec: OciRuntimeContainerSpec) -> dict[str, JsonValue]:
    record = _JSON_OBJECT.validate_json(spec.model_dump_json())
    document = record.get("spec")
    if not isinstance(document, dict):
        msg = "OCI runtime spec must contain a JSON object"
        raise ValueError(msg)
    return document

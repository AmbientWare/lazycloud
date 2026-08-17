from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from foundation.process import (
    ManagedCommand,
    ManagedCommandResult,
    ProcessOutputSink,
    ProcessResult,
    ProcessTimeoutError,
    run_process,
    start_managed_command,
)
from pydantic import JsonValue
from shared.app_identity import CONTAINER_HELPER_PATH, WORKER_BUNDLE_ROOT
from shared.container_requests import StopContainerReason
from shared.image_building.authoring import LinuxArchitecture

import worker.oci_spec
from worker.container_client.models import ContainerExecResponse
from worker.container_execution import (
    ContainerExecutionContext,
    ContainerMountSetupResult,
    ContainerNetworkSetupResult,
    ContainerRuntimeRunResult,
    ContainerRuntimeStartError,
)
from worker.container_rootfs import ContainerRootfsSetupResult
from worker.execution import (
    CONTAINER_INNER_PORT,
    ContainerEnvironmentRequest,
    ContainerResourceRequest,
    GatewayServiceSettings,
    OciDevice,
    OciMount,
    OciMountType,
    PortBinding,
    build_container_environment,
    env_list_to_map,
    plan_oci_linux_resources,
)
from worker.gpu import ContainerGpuAssignmentResult
from worker.lifecycle import (
    HOST_RESOLV_CONF_PATH,
    WORKER_RESOLV_CONF_PATH,
    required_container_resolv_conf_source,
)
from worker.managed_runtime import (
    MANAGED_RUNTIME_CATALOG_DIGEST_ENV,
    MANAGED_RUNTIME_DIGEST_ENV,
    MANAGED_RUNTIME_STARTUP_KINDS,
    ManagedRuntimePlan,
    managed_runtime_python_version,
    plan_managed_runtime,
)
from worker.managed_runtime_catalog import (
    ManagedRuntimeCatalog,
    load_managed_runtime_catalog,
)
from worker.runtime_config import (
    DEFAULT_CONTAINER_CLI_PATH,
    DEFAULT_CONTAINER_CLI_SOURCE,
    DEFAULT_CONTAINER_TMPFS_SIZE_MIB,
    OciRuntimeName,
    RuntimeAvailability,
    RuntimeAvailabilityStatus,
    RuntimeBinaryConfig,
    RuntimeCommandPlan,
    RuntimeCommandRequest,
    RuntimeContainerStatus,
    RuntimeOperation,
    RuntimeState,
    RuntimeUnavailableError,
    build_base_oci_config,
    normalize_oci_runtime,
    parse_runtime_state,
    plan_runtime_command,
    prepare_oci_spec_for_runtime,
    spec_has_gpu,
)
from worker.sandbox_server import (
    WORKER_CONTAINER_UPLOADS_HOST_PATH,
    WORKER_CONTAINER_UPLOADS_MOUNT_PATH,
)

DEFAULT_WORKER_BUNDLE_ROOT = WORKER_BUNDLE_ROOT
DEFAULT_WORKER_IMAGE_MOUNT_ROOT = "/mnt/images"
OCI_HOSTS_PATH = "/etc/hosts"
OCI_RESOLV_CONF_PATH = "/etc/resolv.conf"
OCI_CONFIG_FILE_NAME = "config.json"
OCI_PROCESS_SPEC_DIR_NAME = "processes"
OCI_NETWORK_FILES_DIR_NAME = "network"
OCI_HOSTS_FILE_NAME = "hosts"
OCI_RESOLV_CONF_FILE_NAME = "resolv.conf"
DEFAULT_RUNTIME_RESTORE_START_TIMEOUT_SECONDS = 30.0
DEFAULT_RUNTIME_RESTORE_START_POLL_SECONDS = 0.05
DEFAULT_RUNTIME_CALLBACK_EXIT_GRACE_SECONDS = 0.25
DEFAULT_RUNTIME_CALLBACK_EXIT_POLL_SECONDS = 0.01
DEFAULT_RUNTIME_DELETE_WAIT_TIMEOUT_SECONDS = 5.0
DEFAULT_RUNTIME_DELETE_WAIT_POLL_SECONDS = 0.05
DEFAULT_RUNTIME_STATE_TIMEOUT_SECONDS = 3.0
DEFAULT_RUNTIME_KILL_TIMEOUT_SECONDS = 5.0
DEFAULT_RUNTIME_DELETE_TIMEOUT_SECONDS = 10.0
DEFAULT_RUNTIME_EXEC_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_RUNTIME_CHECKPOINT_TIMEOUT_SECONDS = 30 * 60.0
DEFAULT_RUNTIME_CONTROL_TERMINATION_TIMEOUT_SECONDS = 2.0
DEFAULT_RUNTIME_CONTROL_MAX_OUTPUT_CHARS = 64 * 1024
SANDBOX_SUPERVISOR_WORKER_PATH = CONTAINER_HELPER_PATH
SANDBOX_SUPERVISOR_CONTAINER_PATH = CONTAINER_HELPER_PATH
SANDBOX_SUPERVISOR_TOKEN_CONTAINER_PATH = "/run/lazycloud/sandbox-supervisor.token"
SANDBOX_SUPERVISOR_CONTROL_DIR_NAME = "sandbox-supervisor"
SANDBOX_SUPERVISOR_TOKEN_FILE_NAME = "token"
OCI_HOST_PREPARED_MOUNT_TYPES = {"bind", "none"}


class ManagedCommandStarter(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        output_sink: ProcessOutputSink | None = None,
    ) -> ManagedCommand: ...


class RuntimeProcessRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        termination_timeout_seconds: float,
        max_output_chars: int,
    ) -> ProcessResult: ...


@dataclass(frozen=True, slots=True)
class OciRuntimeCommandTimeouts:
    state_seconds: float = DEFAULT_RUNTIME_STATE_TIMEOUT_SECONDS
    kill_seconds: float = DEFAULT_RUNTIME_KILL_TIMEOUT_SECONDS
    delete_seconds: float = DEFAULT_RUNTIME_DELETE_TIMEOUT_SECONDS
    exec_seconds: float = DEFAULT_RUNTIME_EXEC_TIMEOUT_SECONDS
    checkpoint_seconds: float = DEFAULT_RUNTIME_CHECKPOINT_TIMEOUT_SECONDS
    termination_seconds: float = DEFAULT_RUNTIME_CONTROL_TERMINATION_TIMEOUT_SECONDS
    max_output_chars: int = DEFAULT_RUNTIME_CONTROL_MAX_OUTPUT_CHARS

    def __post_init__(self) -> None:
        values = {
            "state": self.state_seconds,
            "kill": self.kill_seconds,
            "delete": self.delete_seconds,
            "exec": self.exec_seconds,
            "checkpoint": self.checkpoint_seconds,
            "termination": self.termination_seconds,
        }
        invalid = [name for name, value in values.items() if value <= 0]
        if invalid:
            raise ValueError(f"runtime command timeouts must be positive: {', '.join(invalid)}")
        if self.max_output_chars <= 0:
            raise ValueError("runtime command output limit must be positive")

    def for_operation(self, operation: RuntimeOperation) -> float:
        if operation is RuntimeOperation.State or operation is RuntimeOperation.List:
            return self.state_seconds
        if operation is RuntimeOperation.Kill:
            return self.kill_seconds
        if operation is RuntimeOperation.Delete:
            return self.delete_seconds
        if operation is RuntimeOperation.Exec:
            return self.exec_seconds
        if operation is RuntimeOperation.Checkpoint:
            return self.checkpoint_seconds
        raise ValueError(
            f"runtime operation {operation.value} is not a synchronous control command"
        )


class OciRuntimeCommandFailure(RuntimeError):
    def __init__(
        self,
        *,
        runtime: OciRuntimeName,
        operation: RuntimeOperation,
        container_id: str,
        detail: str,
        exit_code: int | None = None,
    ) -> None:
        self.runtime = runtime
        self.operation = operation
        self.container_id = container_id
        self.detail = detail
        self.exit_code = exit_code
        exit_detail = f" exited {exit_code}" if exit_code is not None else " failed"
        suffix = f": {detail.strip()}" if detail.strip() else ""
        super().__init__(
            f"runtime {runtime.value} {operation.value} for {container_id}{exit_detail}{suffix}"
        )


class OciRuntimeCommandTimeout(OciRuntimeCommandFailure):
    def __init__(
        self,
        *,
        runtime: OciRuntimeName,
        operation: RuntimeOperation,
        container_id: str,
        timeout_seconds: float,
        detail: str,
        process_id: int,
        process_reaped: bool,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.process_id = process_id
        self.process_reaped = process_reaped
        reaping = "" if process_reaped else "; process could not be reaped"
        timeout_detail = f"exceeded {timeout_seconds:g} seconds{reaping}"
        if detail.strip():
            timeout_detail = f"{timeout_detail}: {detail.strip()}"
        super().__init__(
            runtime=runtime,
            operation=operation,
            container_id=container_id,
            detail=timeout_detail,
        )


@dataclass(slots=True)
class OciRuntimeSpecBuilder:
    bundle_root: Path = Path(DEFAULT_WORKER_BUNDLE_ROOT)
    image_mount_root: Path = Path(DEFAULT_WORKER_IMAGE_MOUNT_ROOT)
    runtime_configs: dict[OciRuntimeName, RuntimeBinaryConfig] | None = None
    command: list[str] | None = None
    cwd: str = "/workspace"
    readonly_rootfs: bool = False
    docker_enabled: bool = False
    gateway_settings: GatewayServiceSettings = field(default_factory=GatewayServiceSettings)
    resolv_conf_source: Path = Path(HOST_RESOLV_CONF_PATH)
    fallback_resolv_conf_source: Path = Path(WORKER_RESOLV_CONF_PATH)
    container_cli_source: Path | None = Path(DEFAULT_CONTAINER_CLI_SOURCE)
    container_cli_path: str = DEFAULT_CONTAINER_CLI_PATH
    sandbox_supervisor_source: Path | None = Path(SANDBOX_SUPERVISOR_WORKER_PATH)
    sandbox_supervisor_path: str = SANDBOX_SUPERVISOR_CONTAINER_PATH
    sandbox_upload_root: Path = Path(WORKER_CONTAINER_UPLOADS_HOST_PATH)
    sandbox_upload_mount_path: str = WORKER_CONTAINER_UPLOADS_MOUNT_PATH
    managed_runtime_root: Path | None = None
    storage_mount_hosts: bool = True
    mount_worker_resolv_conf: bool = True
    _managed_runtime_catalogs: dict[tuple[str, LinuxArchitecture], ManagedRuntimeCatalog] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _managed_runtime_catalog_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def build_spec(
        self,
        context: ContainerExecutionContext,
        *,
        bind_ports: list[int],
        port_bindings: list[PortBinding],
        mount_result: ContainerMountSetupResult | None = None,
        network_result: ContainerNetworkSetupResult | None = None,
        gpu_result: ContainerGpuAssignmentResult | None = None,
        rootfs_result: ContainerRootfsSetupResult | None = None,
    ) -> worker.oci_spec.OciRuntimeContainerSpec:
        _ = bind_ports
        runtime_config = _select_runtime_config(context.runtime, self.runtime_configs)
        bundle_path = self.bundle_root / context.request.container_id
        root_path = self._root_path(context, rootfs_result)
        env = self._runtime_env(
            context,
            bind_ports=bind_ports,
            network_result=network_result,
        )
        original_command = context.entrypoint or self.command
        managed_command = original_command or []
        catalog = None
        if context.startup_kind in MANAGED_RUNTIME_STARTUP_KINDS:
            catalog = self._managed_runtime_catalog(
                managed_runtime_python_version(managed_command),
                context.architecture,
            )
        managed_runtime = plan_managed_runtime(
            context.startup_kind,
            catalog=catalog,
            architecture=context.architecture,
            command=managed_command,
            user_code_path=context.cwd or self.cwd,
        )
        if gpu_result is not None and gpu_result.env:
            env.update(env_list_to_map(gpu_result.env))
        if managed_runtime.enabled:
            env["PYTHONPATH"] = managed_runtime.python_path(env.get("PYTHONPATH", ""))
            env["PYTHONSAFEPATH"] = "1"
            env[MANAGED_RUNTIME_CATALOG_DIGEST_ENV] = managed_runtime.digest
            env[MANAGED_RUNTIME_DIGEST_ENV] = managed_runtime.artifact_digest
        spec = build_base_oci_config(
            context.runtime,
            root_path=root_path,
            command=managed_runtime.command if managed_runtime.enabled else original_command,
            env=env,
            cwd=context.cwd or self.cwd,
            hostname=context.request.container_id,
            readonly_rootfs=self.readonly_rootfs,
            container_cli_source=self._container_cli_source(),
            container_cli_path=self.container_cli_path,
            tmpfs_size_mib=_container_tmpfs_size_mib(context.request.memory_mib),
        )
        supervisor_token_path = self._apply_sandbox_supervisor(context, spec, bundle_path)
        self._apply_sandbox_upload_mount(context, spec)
        self._apply_managed_runtime(spec, managed_runtime)
        self._apply_resources(context, spec)
        self._apply_network(spec, network_result)
        self._apply_mounts(spec, mount_result)
        self._apply_gpu(spec, gpu_result)
        self._apply_runtime_network_files(context, spec, bundle_path)
        self._annotate_ports(spec, port_bindings)
        return worker.oci_spec.OciRuntimeContainerSpec(
            container_id=context.request.container_id,
            runtime=runtime_config,
            bundle_path=str(bundle_path),
            config_path=str(bundle_path / OCI_CONFIG_FILE_NAME),
            process_spec_dir=str(bundle_path / OCI_PROCESS_SPEC_DIR_NAME),
            sandbox_supervisor_token_path=supervisor_token_path,
            spec=spec,
            docker_enabled=context.docker_enabled or self.docker_enabled,
        )

    def _managed_runtime_catalog(
        self,
        python_version: str,
        architecture: LinuxArchitecture,
    ) -> ManagedRuntimeCatalog:
        root = self.managed_runtime_root
        if root is None:
            raise RuntimeError("managed runtime artifact catalog is unavailable")
        with self._managed_runtime_catalog_lock:
            key = (python_version, architecture)
            catalog = self._managed_runtime_catalogs.get(key)
            if catalog is None:
                catalog = load_managed_runtime_catalog(
                    root,
                    python_version,
                    architecture,
                )
                self._managed_runtime_catalogs[key] = catalog
            return catalog

    def _apply_managed_runtime(
        self,
        spec: dict[str, JsonValue],
        plan: ManagedRuntimePlan,
    ) -> None:
        if plan.enabled:
            self._extend_mounts(spec, plan.mounts)

    def _apply_sandbox_supervisor(
        self,
        context: ContainerExecutionContext,
        spec: dict[str, JsonValue],
        bundle_path: Path,
    ) -> str:
        source = self.sandbox_supervisor_source
        if source is None or not source.is_file():
            if context.request.stub_type != "sandbox":
                return ""
            raise RuntimeError("sandbox supervisor binary is unavailable on this worker")
        self._extend_mounts(
            spec,
            [
                OciMount(
                    mount_type=OciMountType.Bind,
                    source=str(source),
                    destination=self.sandbox_supervisor_path,
                    options=["ro", "rbind", "rprivate", "nosuid", "nodev"],
                )
            ],
        )
        if context.request.stub_type != "sandbox":
            return ""
        process = spec.get("process")
        if not isinstance(process, dict):
            raise RuntimeError("sandbox OCI process configuration is missing")
        process["args"] = [self.sandbox_supervisor_path]
        token_path = (
            bundle_path / SANDBOX_SUPERVISOR_CONTROL_DIR_NAME / SANDBOX_SUPERVISOR_TOKEN_FILE_NAME
        )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        token_path.chmod(0o600)
        self._extend_mounts(
            spec,
            [
                OciMount(
                    mount_type=OciMountType.Bind,
                    source=str(token_path),
                    destination=SANDBOX_SUPERVISOR_TOKEN_CONTAINER_PATH,
                    options=["ro", "rbind", "rprivate", "nosuid", "noexec", "nodev"],
                ),
            ],
        )
        return str(token_path)

    def _apply_sandbox_upload_mount(
        self,
        context: ContainerExecutionContext,
        spec: dict[str, JsonValue],
    ) -> None:
        if context.request.stub_type != "sandbox" or context.runtime is not OciRuntimeName.Runsc:
            return
        source = self.sandbox_upload_root / context.request.container_id
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        source.chmod(0o700)
        self._extend_mounts(
            spec,
            [
                OciMount(
                    mount_type=OciMountType.Bind,
                    source=str(source),
                    destination=self.sandbox_upload_mount_path,
                    options=["rw", "rbind", "rprivate", "nosuid", "nodev", "noexec"],
                )
            ],
        )

    def _root_path(
        self,
        context: ContainerExecutionContext,
        rootfs_result: ContainerRootfsSetupResult | None = None,
    ) -> str:
        # The per-container overlay merged view is the only correct writable root:
        # the image mount root is shared by every container running that image.
        if rootfs_result is not None and rootfs_result.prepared and rootfs_result.root_path:
            return rootfs_result.root_path
        if context.request.image_id:
            msg = (
                "container "
                f"{context.request.container_id} has image "
                f"{context.request.image_id} but no prepared root filesystem; refusing to "
                "share the image directory as a writable root"
            )
            raise RuntimeError(msg)
        return "rootfs"

    def _container_cli_source(self) -> str | None:
        if self.container_cli_source is None:
            return None
        if not self.container_cli_source.exists():
            return None
        return str(self.container_cli_source)

    def _runtime_env(
        self,
        context: ContainerExecutionContext,
        *,
        bind_ports: list[int],
        network_result: ContainerNetworkSetupResult | None,
    ) -> dict[str, str]:
        identity = network_result.identity if network_result is not None else None
        env_plan = build_container_environment(
            ContainerEnvironmentRequest(
                container_id=context.request.container_id,
                pod_address=(
                    identity.pod_address
                    if identity is not None and identity.pod_address
                    else context.request.container_id
                ),
                workspace_id=context.request.workspace_id,
                workspace_name=context.request.workspace_name,
                bind_ports=bind_ports or [CONTAINER_INNER_PORT],
                storage_available=context.request.workspace_storage_available,
                request_env=list(context.request.env),
            ),
            self.gateway_settings,
        )
        return env_plan.env_map

    def _apply_resources(
        self,
        context: ContainerExecutionContext,
        spec: dict[str, JsonValue],
    ) -> None:
        if context.request.cpu_millicores <= 0 or context.request.memory_mib <= 0:
            return
        resources = plan_oci_linux_resources(
            ContainerResourceRequest(
                cpu_millicores=context.request.cpu_millicores,
                memory_mib=context.request.memory_mib,
                memory_enforced=context.memory_enforced,
            )
        )
        linux = spec.get("linux")
        if not isinstance(linux, dict):
            linux = {}
            spec["linux"] = linux
        linux["resources"] = resources.as_oci_dict()

    def _apply_network(
        self,
        spec: dict[str, JsonValue],
        network_result: ContainerNetworkSetupResult | None,
    ) -> None:
        if network_result is None or not network_result.namespace_path:
            return
        linux = spec.get("linux")
        if not isinstance(linux, dict):
            linux = {}
            spec["linux"] = linux
        namespaces = linux.get("namespaces")
        if not isinstance(namespaces, list):
            namespaces = []
            linux["namespaces"] = namespaces
        namespaces.append({"type": "network", "path": network_result.namespace_path})

    def _apply_mounts(
        self,
        spec: dict[str, JsonValue],
        mount_result: ContainerMountSetupResult | None,
    ) -> None:
        if mount_result is None or not mount_result.oci_mounts:
            return
        self._extend_mounts(spec, mount_result.oci_mounts)

    def _apply_gpu(
        self,
        spec: dict[str, JsonValue],
        gpu_result: ContainerGpuAssignmentResult | None,
    ) -> None:
        if gpu_result is None or not gpu_result.assigned_devices:
            return
        if gpu_result.oci_mounts:
            self._extend_mounts(spec, gpu_result.oci_mounts)
        # The devices are the whole point. Annotations below describe the
        # assignment; only these let the container open a GPU, and `/dev` is a
        # fresh tmpfs so nothing arrives by inheritance.
        if gpu_result.oci_devices:
            self._extend_devices(spec, gpu_result.oci_devices)
        annotations = spec.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
            spec["annotations"] = annotations
        annotations["runtime.nvidia.assigned_devices"] = ",".join(
            str(device) for device in gpu_result.assigned_devices
        )
        if gpu_result.cdi_devices:
            annotations["cdi.k8s.io/devices"] = ",".join(gpu_result.cdi_devices)

    def _apply_runtime_network_files(
        self,
        context: ContainerExecutionContext,
        spec: dict[str, JsonValue],
        bundle_path: Path,
    ) -> None:
        mounts: list[OciMount] = []
        hosts_path = self._prepare_hosts_file(context, bundle_path)
        if hosts_path is not None and not _mount_destination_exists(spec, OCI_HOSTS_PATH):
            mounts.append(_readonly_file_mount(hosts_path, OCI_HOSTS_PATH))
        resolv_path = self._prepare_resolv_conf_file(bundle_path)
        if resolv_path is not None and not _mount_destination_exists(spec, OCI_RESOLV_CONF_PATH):
            mounts.append(_readonly_file_mount(resolv_path, OCI_RESOLV_CONF_PATH))
        if mounts:
            self._extend_mounts(spec, mounts)

    def _prepare_hosts_file(
        self,
        context: ContainerExecutionContext,
        bundle_path: Path,
    ) -> Path | None:
        if not self.storage_mount_hosts:
            return None
        entries = runtime_hosts_entries(context.request.container_id)
        path = _network_files_dir(bundle_path) / OCI_HOSTS_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(entries) + "\n", encoding="utf-8")
        return path

    def _prepare_resolv_conf_file(self, bundle_path: Path) -> Path | None:
        if not self.mount_worker_resolv_conf:
            return None
        source = required_container_resolv_conf_source(
            host_path=str(self.resolv_conf_source),
            fallback_path=str(self.fallback_resolv_conf_source),
        )
        path = _network_files_dir(bundle_path) / OCI_RESOLV_CONF_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return path

    def _extend_devices(
        self,
        spec: dict[str, JsonValue],
        devices_to_add: list[OciDevice],
    ) -> None:
        """Add the device nodes and the cgroup rules that permit opening them.

        Both halves are required. A node without its rule is visible and
        unopenable, and the failure surfaces inside the workload as a CUDA
        initialisation error rather than as anything naming permissions.
        """
        linux = spec.get("linux")
        if not isinstance(linux, dict):
            linux = {}
            spec["linux"] = linux
        devices = linux.get("devices")
        if not isinstance(devices, list):
            devices = []
            linux["devices"] = devices
        present = {
            str(device.get("path"))
            for device in devices
            if isinstance(device, dict) and device.get("path") is not None
        }
        resources = linux.get("resources")
        if not isinstance(resources, dict):
            resources = {}
            linux["resources"] = resources
        allowed = resources.get("devices")
        if not isinstance(allowed, list):
            allowed = []
            resources["devices"] = allowed
        for device in devices_to_add:
            if device.path in present:
                continue
            devices.append(device.as_oci_dict())
            allowed.append(device.as_cgroup_allow())

    def _extend_mounts(
        self,
        spec: dict[str, JsonValue],
        mounts_to_add: list[OciMount],
    ) -> None:
        mounts = spec.get("mounts")
        if not isinstance(mounts, list):
            mounts = []
            spec["mounts"] = mounts
        mounts.extend(mount.as_oci_dict() for mount in mounts_to_add)

    def _annotate_ports(
        self,
        spec: dict[str, JsonValue],
        port_bindings: list[PortBinding],
    ) -> None:
        if not port_bindings:
            return
        annotations = spec.get("annotations")
        if not isinstance(annotations, dict):
            annotations = {}
            spec["annotations"] = annotations
        annotations["runtime.container_ports"] = ",".join(
            f"{binding.container_port}:{binding.host_port}" for binding in port_bindings
        )


def runtime_hosts_entries(
    container_id: str,
) -> list[str]:
    rows = ["127.0.0.1 localhost", "::1 localhost ip6-localhost ip6-loopback"]
    if container_id:
        rows.append(f"127.0.1.1 {container_id}")
    return rows


def _network_files_dir(bundle_path: Path) -> Path:
    return bundle_path / OCI_NETWORK_FILES_DIR_NAME


def _readonly_file_mount(source: Path, destination: str) -> OciMount:
    return OciMount(
        mount_type=OciMountType.Bind,
        source=str(source),
        destination=destination,
        options=["ro", "rbind", "rprivate", "nosuid", "noexec", "nodev"],
    )


def _container_tmpfs_size_mib(memory_mib: int) -> int:
    """Bound in-container tmpfs by the container's own memory request.

    tmpfs pages are charged to the allocating memory cgroup, so sizing these from
    the request keeps a container's tmpfs use inside the budget it asked for
    instead of letting it reach the whole memory ceiling.
    """
    if memory_mib <= 0:
        return DEFAULT_CONTAINER_TMPFS_SIZE_MIB
    return max(DEFAULT_CONTAINER_TMPFS_SIZE_MIB, memory_mib // 2)


def _mount_destination_exists(spec: dict[str, JsonValue], destination: str) -> bool:
    mounts = spec.get("mounts")
    if not isinstance(mounts, list):
        return False
    return any(
        isinstance(mount, dict) and mount.get("destination") == destination for mount in mounts
    )


def _prepare_oci_rootfs_paths(spec: dict[str, JsonValue], *, bundle_path: str) -> None:
    root_path = _oci_rootfs_path(spec, bundle_path)
    if root_path is None:
        return
    mounts = spec.get("mounts")
    if isinstance(mounts, list):
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            _prepare_oci_mount_destination(root_path, mount)
    process = spec.get("process")
    if isinstance(process, dict):
        cwd = process.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/") and cwd != "/":
            (root_path / cwd.lstrip("/")).mkdir(parents=True, exist_ok=True)


def _oci_rootfs_path(spec: dict[str, JsonValue], bundle_path: str) -> Path | None:
    root = spec.get("root")
    if not isinstance(root, dict):
        return None
    raw_path = root.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return Path(bundle_path) / path


def _prepare_oci_mount_destination(
    root_path: Path,
    mount: Mapping[str, JsonValue],
) -> None:
    mount_type = mount.get("type")
    if mount_type not in OCI_HOST_PREPARED_MOUNT_TYPES:
        return
    raw_source = mount.get("source")
    raw_destination = mount.get("destination")
    if not isinstance(raw_source, str) or not isinstance(raw_destination, str):
        return
    if not raw_destination.startswith("/") or raw_destination == "/":
        return
    source = Path(raw_source)
    if not source.exists():
        return
    destination = root_path / raw_destination.lstrip("/")
    if source.is_dir():
        if destination.exists() and not destination.is_dir():
            msg = f"OCI mount destination is not a directory: {raw_destination}"
            raise RuntimeError(msg)
        destination.mkdir(parents=True, exist_ok=True)
        return
    if destination.exists() and destination.is_dir():
        msg = f"OCI mount destination is not a file: {raw_destination}"
        raise RuntimeError(msg)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.touch(exist_ok=True)


@dataclass(slots=True)
class OciRuntimeCommandController:
    run_command: RuntimeProcessRunner = run_process
    start_command: ManagedCommandStarter = start_managed_command
    command_timeouts: OciRuntimeCommandTimeouts = field(default_factory=OciRuntimeCommandTimeouts)
    runtime_config: RuntimeBinaryConfig = field(default_factory=RuntimeBinaryConfig)
    runtime_configs: dict[OciRuntimeName, RuntimeBinaryConfig] | None = None
    container_runtime: Callable[[str], OciRuntimeName | None] | None = None
    cuda_checkpoint_path: str | None = None
    restore_start_timeout_seconds: float = DEFAULT_RUNTIME_RESTORE_START_TIMEOUT_SECONDS
    restore_start_poll_seconds: float = DEFAULT_RUNTIME_RESTORE_START_POLL_SECONDS
    delete_wait_timeout_seconds: float = DEFAULT_RUNTIME_DELETE_WAIT_TIMEOUT_SECONDS
    delete_wait_poll_seconds: float = DEFAULT_RUNTIME_DELETE_WAIT_POLL_SECONDS
    prepared_specs: dict[str, worker.oci_spec.OciRuntimeContainerSpec] = field(default_factory=dict)
    requested_stop_reasons: dict[str, StopContainerReason] = field(default_factory=dict)
    stop_reason_lock: threading.Lock = field(default_factory=threading.Lock)

    def record_stop_reason(
        self,
        container_id: str,
        reason: StopContainerReason,
    ) -> None:
        with self.stop_reason_lock:
            self.requested_stop_reasons.setdefault(container_id, reason)

    def _consume_stop_reason(self, container_id: str) -> StopContainerReason:
        with self.stop_reason_lock:
            return self.requested_stop_reasons.pop(
                container_id,
                StopContainerReason.Unknown,
            )

    def prepare(self, spec: worker.oci_spec.OciRuntimeContainerSpec) -> None:
        prepared = prepare_oci_spec_for_runtime(
            spec.spec,
            spec.runtime.runtime,
            cuda_checkpoint_path=self.cuda_checkpoint_path,
            docker_enabled=spec.docker_enabled,
        )
        _prepare_oci_rootfs_paths(prepared.spec, bundle_path=spec.bundle_path)
        config_path = Path(spec.config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        Path(spec.process_spec_dir).mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(prepared.spec, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.prepared_specs[spec.container_id] = spec

    def run(
        self,
        spec: worker.oci_spec.OciRuntimeContainerSpec,
        *,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
    ) -> ContainerRuntimeRunResult:
        plan = plan_runtime_command(
            spec.runtime,
            RuntimeCommandRequest(
                operation=RuntimeOperation.Run,
                container_id=spec.container_id,
                bundle_path=spec.bundle_path,
                docker_enabled=spec.docker_enabled,
                nvproxy=spec_has_gpu(spec.spec),
            ),
        )
        command = self.start_command(plan.argv, output_sink=output_sink)
        started_pid, completed = self._wait_for_running_container(
            spec.container_id,
            command,
            operation="run",
        )
        if started_pid is not None:
            try:
                on_started(started_pid)
            except Exception as callback_error:
                completed_during_callback = self._wait_for_callback_exit(command)
                self._abort_started_command(
                    spec.container_id,
                    command,
                    cleanup_argv=plan.cleanup_argv,
                    callback_error=callback_error,
                )
                if completed_during_callback is not None:
                    raise ContainerRuntimeStartError(
                        spec.container_id,
                        exit_code=completed_during_callback.exit_code,
                        output=completed_during_callback.output,
                    ) from callback_error
                raise
        result = completed or command.wait()
        if plan.cleanup_argv is not None:
            self._delete_runtime_container(
                spec.container_id,
                cleanup_argv=plan.cleanup_argv,
            )
        return ContainerRuntimeRunResult(
            exit_code=result.exit_code,
            stop_reason=self._consume_stop_reason(spec.container_id),
            started_pid=started_pid,
            oom_killed=False,
            output=result.output,
        )

    def status(self, container_id: str) -> str:
        state = self._runtime_state(container_id)
        if state is None:
            return RuntimeContainerStatus.Stopped.value
        return state.status.value

    def exec_container(
        self,
        container_id: str,
        *,
        argv: list[str],
        env: list[str],
        cwd: str,
    ) -> ContainerExecResponse:
        spec_dir = self._process_spec_dir(container_id)
        spec_dir.mkdir(parents=True, exist_ok=True)
        process_spec_path = spec_dir / f"{uuid4().hex}.json"
        process_spec_path.write_text(
            json.dumps(
                {
                    "terminal": False,
                    "args": argv,
                    "env": env,
                    "cwd": cwd,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        runtime_config = self._runtime_config(container_id)
        plan = plan_runtime_command(
            runtime_config,
            RuntimeCommandRequest(
                operation=RuntimeOperation.Exec,
                container_id=container_id,
                process_spec_path=str(process_spec_path),
            ),
        )
        try:
            result = self._run_control_command(container_id, runtime_config, plan)
        except OciRuntimeCommandFailure as exc:
            return ContainerExecResponse(
                ok=False,
                exit_code=-1,
                error_msg=str(exc),
            )
        return ContainerExecResponse(
            ok=result.ok,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            error_msg="" if result.ok else result.stderr or result.stdout,
        )

    def kill_container(self, container_id: str, *, signal: int, force_delete: bool) -> None:
        runtime_config = self._runtime_config(container_id)
        initial_state = self._runtime_state(container_id)
        if initial_state is None:
            return
        initial_status = initial_state.status
        kill = plan_runtime_command(
            runtime_config,
            RuntimeCommandRequest(
                operation=RuntimeOperation.Kill,
                container_id=container_id,
                signal=signal,
                all_processes=True,
            ),
        )
        if initial_status is not RuntimeContainerStatus.Stopped:
            try:
                result = self._run_control_command(container_id, runtime_config, kill)
            except OciRuntimeCommandFailure:
                if self._runtime_is_absent_or_stopped(container_id):
                    result = None
                else:
                    raise
            if result is not None and not result.ok:
                if self._result_reports_missing(runtime_config, kill.operation, result):
                    return
                if not self._runtime_is_absent_or_stopped(container_id):
                    raise self._command_failure(
                        container_id, runtime_config, kill.operation, result
                    )
        if force_delete:
            last_status = initial_status
            deadline = time.monotonic() + self.delete_wait_timeout_seconds
            while last_status is not RuntimeContainerStatus.Stopped and time.monotonic() < deadline:
                state = self._runtime_state(container_id)
                if state is None:
                    return
                last_status = state.status
                if last_status is RuntimeContainerStatus.Stopped:
                    break
                time.sleep(self.delete_wait_poll_seconds)
            delete = plan_runtime_command(
                runtime_config,
                RuntimeCommandRequest(
                    operation=RuntimeOperation.Delete,
                    container_id=container_id,
                    force=True,
                ),
            )
            self._delete_runtime_container(
                container_id,
                cleanup_argv=delete.argv,
                last_status=last_status,
            )

    def checkpoint_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        leave_running: bool = True,
        allow_open_tcp: bool = True,
        skip_in_flight: bool = True,
        link_remap: bool = True,
    ) -> None:
        Path(image_path).mkdir(parents=True, exist_ok=True)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        plan = plan_runtime_command(
            self._runtime_config(container_id),
            RuntimeCommandRequest(
                operation=RuntimeOperation.Checkpoint,
                container_id=container_id,
                image_path=image_path,
                work_dir=work_dir,
                leave_running=leave_running,
                allow_open_tcp=allow_open_tcp,
                skip_in_flight=skip_in_flight,
                link_remap=link_remap,
            ),
        )
        result = self._run_control_command(
            container_id,
            self._runtime_config(container_id),
            plan,
        )
        if not result.ok:
            raise self._command_failure(
                container_id,
                self._runtime_config(container_id),
                plan.operation,
                result,
            )

    def restore_container(
        self,
        container_id: str,
        *,
        image_path: str,
        work_dir: str,
        bundle_path: str,
        on_started: Callable[[int], None],
        output_sink: ProcessOutputSink | None = None,
        tcp_close: bool = True,
        link_remap: bool = True,
    ) -> ContainerRuntimeRunResult:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        plan = plan_runtime_command(
            self._runtime_config(container_id),
            RuntimeCommandRequest(
                operation=RuntimeOperation.Restore,
                container_id=container_id,
                image_path=image_path,
                work_dir=work_dir,
                bundle_path=bundle_path,
                tcp_close=tcp_close,
                link_remap=link_remap,
            ),
        )
        command = self.start_command(plan.argv, output_sink=output_sink)
        started_pid, completed = self._wait_for_running_container(
            container_id,
            command,
            operation="restore",
        )
        if started_pid is None:
            detail = completed.output.strip() if completed is not None else ""
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"runtime restore exited before starting {container_id}{suffix}")
        try:
            on_started(started_pid)
        except Exception as callback_error:
            completed_during_callback = self._wait_for_callback_exit(command)
            self._abort_started_command(
                container_id,
                command,
                cleanup_argv=plan.cleanup_argv,
                callback_error=callback_error,
            )
            if completed_during_callback is not None:
                raise ContainerRuntimeStartError(
                    container_id,
                    exit_code=completed_during_callback.exit_code,
                    output=completed_during_callback.output,
                ) from callback_error
            raise
        result = completed or command.wait()
        if plan.cleanup_argv is not None:
            self._delete_runtime_container(
                container_id,
                cleanup_argv=plan.cleanup_argv,
            )
        if not result.ok:
            msg = result.output or f"runtime restore failed for {container_id}"
            raise RuntimeError(msg)
        return ContainerRuntimeRunResult(
            exit_code=result.exit_code,
            stop_reason=self._consume_stop_reason(container_id),
            started_pid=started_pid,
            output=result.output,
        )

    def _abort_started_command(
        self,
        container_id: str,
        command: ManagedCommand,
        *,
        cleanup_argv: list[str] | None,
        callback_error: Exception,
    ) -> None:
        abort_errors: list[tuple[str, Exception]] = []
        try:
            command.terminate(timeout_seconds=self.command_timeouts.termination_seconds)
        except Exception as exc:
            abort_errors.append(("launch termination", exc))

        delete_argv = (
            cleanup_argv
            or plan_runtime_command(
                self._runtime_config(container_id),
                RuntimeCommandRequest(
                    operation=RuntimeOperation.Delete,
                    container_id=container_id,
                    force=True,
                ),
            ).argv
        )
        try:
            self._delete_runtime_container(
                container_id,
                cleanup_argv=delete_argv,
            )
        except OciRuntimeCommandFailure as exc:
            abort_errors.append(("forced runtime delete", exc))

        if not abort_errors:
            try:
                self._wait_for_container_absence(container_id)
            except RuntimeError as exc:
                abort_errors.append(("runtime absence verification", exc))
        if abort_errors:
            if len(abort_errors) == 1 and isinstance(abort_errors[0][1], OciRuntimeCommandFailure):
                raise abort_errors[0][1] from callback_error
            detail = "; ".join(f"{stage} failed: {error}" for stage, error in abort_errors)
            raise RuntimeError(
                f"runtime start callback failed for {container_id} and cleanup was incomplete: "
                f"{detail}"
            ) from callback_error

    @staticmethod
    def _wait_for_callback_exit(command: ManagedCommand) -> ManagedCommandResult | None:
        deadline = time.monotonic() + DEFAULT_RUNTIME_CALLBACK_EXIT_GRACE_SECONDS
        while True:
            completed = command.poll()
            if completed is not None:
                return completed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(DEFAULT_RUNTIME_CALLBACK_EXIT_POLL_SECONDS, remaining))

    def _wait_for_running_container(
        self,
        container_id: str,
        command: ManagedCommand,
        *,
        operation: str,
    ) -> tuple[int | None, ManagedCommandResult | None]:
        deadline = time.monotonic() + self.restore_start_timeout_seconds
        state_plan = plan_runtime_command(
            self._runtime_config(container_id),
            RuntimeCommandRequest(
                operation=RuntimeOperation.State,
                container_id=container_id,
            ),
        )
        last_state_error = ""
        while time.monotonic() < deadline:
            try:
                state = self._runtime_state(container_id, plan=state_plan)
            except OciRuntimeCommandFailure as exc:
                last_state_error = str(exc)
            else:
                if (
                    state is not None
                    and state.status is RuntimeContainerStatus.Running
                    and state.pid > 0
                ):
                    return state.pid, None

            completed = command.poll()
            if completed is not None:
                if completed.ok:
                    return None, completed
                msg = completed.output or f"runtime {operation} failed for {container_id}"
                raise RuntimeError(msg)
            time.sleep(self.restore_start_poll_seconds)

        command.terminate(timeout_seconds=self.command_timeouts.termination_seconds)
        detail = f": {last_state_error.strip()}" if last_state_error.strip() else ""
        raise RuntimeError(f"runtime {operation} did not start for {container_id}{detail}")

    def _wait_for_container_absence(self, container_id: str) -> None:
        deadline = time.monotonic() + self.delete_wait_timeout_seconds
        while time.monotonic() < deadline:
            if self._runtime_state(container_id) is None:
                return
            time.sleep(self.delete_wait_poll_seconds)
        runtime_config = self._runtime_config(container_id)
        raise OciRuntimeCommandFailure(
            runtime=runtime_config.runtime,
            operation=RuntimeOperation.Delete,
            container_id=container_id,
            detail="container remained after forced delete",
        )

    def _run_control_command(
        self,
        container_id: str,
        runtime_config: RuntimeBinaryConfig,
        plan: RuntimeCommandPlan,
    ) -> ProcessResult:
        timeout_seconds = self.command_timeouts.for_operation(plan.operation)
        try:
            return self.run_command(
                plan.argv,
                timeout_seconds=timeout_seconds,
                termination_timeout_seconds=self.command_timeouts.termination_seconds,
                max_output_chars=self.command_timeouts.max_output_chars,
            )
        except ProcessTimeoutError as exc:
            detail = exc.stderr or exc.stdout
            raise OciRuntimeCommandTimeout(
                runtime=runtime_config.runtime,
                operation=plan.operation,
                container_id=container_id,
                timeout_seconds=timeout_seconds,
                detail=detail,
                process_id=exc.pid,
                process_reaped=exc.termination_error is None,
            ) from exc
        except OSError as exc:
            raise OciRuntimeCommandFailure(
                runtime=runtime_config.runtime,
                operation=plan.operation,
                container_id=container_id,
                detail=str(exc),
            ) from exc

    def _runtime_state(
        self,
        container_id: str,
        *,
        plan: RuntimeCommandPlan | None = None,
    ) -> RuntimeState | None:
        runtime_config = self._runtime_config(container_id)
        state_plan = plan or plan_runtime_command(
            runtime_config,
            RuntimeCommandRequest(
                operation=RuntimeOperation.State,
                container_id=container_id,
            ),
        )
        result = self._run_control_command(container_id, runtime_config, state_plan)
        if not result.ok:
            if self._result_reports_missing(runtime_config, state_plan.operation, result):
                return None
            raise self._command_failure(
                container_id,
                runtime_config,
                state_plan.operation,
                result,
            )
        if not result.stdout.strip():
            raise OciRuntimeCommandFailure(
                runtime=runtime_config.runtime,
                operation=RuntimeOperation.State,
                container_id=container_id,
                detail="runtime returned an empty state response",
                exit_code=result.exit_code,
            )
        try:
            return parse_runtime_state(result.stdout)
        except (TypeError, ValueError) as exc:
            raise OciRuntimeCommandFailure(
                runtime=runtime_config.runtime,
                operation=RuntimeOperation.State,
                container_id=container_id,
                detail=f"invalid runtime state response: {exc}",
                exit_code=result.exit_code,
            ) from exc

    def _runtime_is_absent_or_stopped(self, container_id: str) -> bool:
        state = self._runtime_state(container_id)
        return state is None or state.status is RuntimeContainerStatus.Stopped

    def _delete_runtime_container(
        self,
        container_id: str,
        *,
        cleanup_argv: list[str],
        last_status: RuntimeContainerStatus | None = None,
    ) -> None:
        runtime_config = self._runtime_config(container_id)
        delete_plan = RuntimeCommandPlan(
            runtime=runtime_config.runtime,
            operation=RuntimeOperation.Delete,
            argv=cleanup_argv,
        )
        try:
            result = self._run_control_command(container_id, runtime_config, delete_plan)
        except OciRuntimeCommandFailure as command_error:
            if self._runtime_state(container_id) is None:
                return
            raise command_error
        if result.ok or self._result_reports_missing(
            runtime_config,
            RuntimeOperation.Delete,
            result,
        ):
            return
        if self._runtime_state(container_id) is None:
            return
        failure = self._command_failure(
            container_id,
            runtime_config,
            RuntimeOperation.Delete,
            result,
        )
        if last_status is None:
            raise failure
        raise OciRuntimeCommandFailure(
            runtime=runtime_config.runtime,
            operation=RuntimeOperation.Delete,
            container_id=container_id,
            detail=f"container state was {last_status.value}: {failure.detail}",
            exit_code=failure.exit_code,
        )

    @staticmethod
    def _result_reports_missing(
        runtime_config: RuntimeBinaryConfig,
        operation: RuntimeOperation,
        result: ProcessResult,
    ) -> bool:
        if (
            runtime_config.runtime is OciRuntimeName.Runsc
            and operation is RuntimeOperation.State
            and result.exit_code == 1
        ):
            return True
        detail = f"{result.stderr}\n{result.stdout}".lower()
        if (
            runtime_config.runtime is OciRuntimeName.Runsc
            and operation is RuntimeOperation.State
            and (
                "loading container: file does not exist" in detail
                # The same absence, reported by the OS rather than by runsc: the
                # state file it wants to open is not there. Recognised because a
                # container that has already exited is exactly what a stop asks
                # about, and reading this as a runtime fault fails the shutdown
                # acknowledgement a delete waits on.
                or ("loading container: open" in detail and "no such file" in detail)
            )
        ):
            return True
        return any(
            marker in detail
            for marker in (
                "container does not exist",
                "container not found",
                "no such container",
            )
        )

    @staticmethod
    def _command_failure(
        container_id: str,
        runtime_config: RuntimeBinaryConfig,
        operation: RuntimeOperation,
        result: ProcessResult,
    ) -> OciRuntimeCommandFailure:
        detail = result.stderr or result.stdout or "runtime returned no error detail"
        return OciRuntimeCommandFailure(
            runtime=runtime_config.runtime,
            operation=operation,
            container_id=container_id,
            detail=detail,
            exit_code=result.exit_code,
        )

    def _runtime_config(self, container_id: str) -> RuntimeBinaryConfig:
        prepared = self.prepared_specs.get(container_id)
        if prepared is not None:
            return prepared.runtime
        if self.container_runtime is not None:
            selected = self.container_runtime(container_id)
            if selected is not None:
                return _select_runtime_config(selected, self.runtime_configs)
        return self.runtime_config

    def _process_spec_dir(self, container_id: str) -> Path:
        prepared = self.prepared_specs.get(container_id)
        if prepared is not None:
            return Path(prepared.process_spec_dir)
        return Path(DEFAULT_WORKER_BUNDLE_ROOT) / container_id / OCI_PROCESS_SPEC_DIR_NAME


def _select_runtime_config(
    runtime: OciRuntimeName,
    configs: dict[OciRuntimeName, RuntimeBinaryConfig] | None,
) -> RuntimeBinaryConfig:
    # A container persisted before the move to gVisor names runc. Resolve it the
    # way every other entry point does: refusing here strands a running container
    # on a worker that advertises only runsc, with no way to reach it again.
    runtime = normalize_oci_runtime(runtime)
    if configs is None:
        return RuntimeBinaryConfig(runtime=runtime)
    config = configs.get(runtime)
    if config is not None:
        return config
    raise RuntimeUnavailableError(
        RuntimeAvailability(
            runtime=runtime,
            status=RuntimeAvailabilityStatus.Missing,
            binary_path=runtime.value,
            reason="worker did not advertise this runtime capability",
        )
    )

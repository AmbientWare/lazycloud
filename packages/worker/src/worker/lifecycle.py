from __future__ import annotations

import ipaddress
import posixpath
from enum import StrEnum
from pathlib import Path

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, field_validator
from shared.container_requests import (
    DEFAULT_ARTIFACTS_PATH,
    DEFAULT_ARTIFACTS_PREFIX,
    DEFAULT_VOLUMES_PATH,
    DEFAULT_VOLUMES_PREFIX,
    DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
    WORKER_CONTAINER_VOLUME_PATH,
    WORKER_USER_ARTIFACT_VOLUME,
    RequestMount,
    RequestMountType,
    WorkerStartupKind,
)
from shared.contracts import ContractModel

from worker.execution import (
    CONTAINER_INNER_PORT,
    OciLinuxCpu,
    OciLinuxMemory,
    OciLinuxResources,
    OciMount,
    OciMountType,
    PortBinding,
)

WORKER_SHELL_PORT = 2222
WORKER_SANDBOX_PROCESS_MANAGER_PORT = 7111
HOST_RESOLV_CONF_PATH = "/etc/resolv.conf"
WORKER_RESOLV_CONF_PATH = "/etc/lazycloud/worker-resolv.conf"
DEFAULT_WORKER_PYTHON_EXECUTABLE = "python3.12"
ENDPOINT_RUNNER_MODULE = "runner.serve"
FUNCTION_RUNNER_MODULE = "runner.function"

type JsonObject = dict[str, JsonValue]

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
HANDLER_ENV_PREFIX = "HANDLER="


class RuntimeStartedWaitAction(StrEnum):
    Await = "await"
    HandlePid = "handle-pid"
    Return = "return"


class RuntimeStartedWaitReason(StrEnum):
    RuntimeStarted = "runtime-started"
    RuntimeDoneDrainedPid = "runtime-done-drained-pid"
    RuntimeDoneNoPid = "runtime-done-no-pid"
    ContextDone = "context-done"
    Waiting = "waiting"


class DeferredCpuApplyAction(StrEnum):
    Noop = "noop"
    Apply = "apply"
    Reject = "reject"


class FallbackEntrypointReason(StrEnum):
    ExplicitConfig = "explicit-config"
    Runner = "runner"
    MissingHandler = "missing-handler"
    UnsupportedKind = "unsupported-kind"
    InvalidConfig = "invalid-config"


class RequestMountPrepareAction(StrEnum):
    Mount = "mount"
    Skip = "skip"


class RequestMountPrepareReason(StrEnum):
    BindSourceReady = "bind-source-ready"
    EmptyLocalPath = "empty-local-path"
    MountPointExists = "mountpoint-exists"
    MountPointMissing = "mountpoint-missing"


class BindMountSourceDirAction(StrEnum):
    Create = "create"
    Skip = "skip"


class BindMountSourceDirReason(StrEnum):
    BindMountSource = "bind-mount-source"
    MountPoint = "mountpoint"
    EmptyLocalPath = "empty-local-path"


class ContainerStartupPortRequest(ContractModel):
    kind: WorkerStartupKind = WorkerStartupKind.Unknown
    ports: list[int] = Field(default_factory=list)
    requested_ports: list[int] = Field(default_factory=list)
    checkpoint_exposed_ports: list[int] = Field(default_factory=list)

    @field_validator("ports", "requested_ports", "checkpoint_exposed_ports")
    @classmethod
    def ports_must_be_valid(cls, value: list[int]) -> list[int]:
        for port in value:
            if not 1 <= port <= 65535:
                msg = "startup ports must be between 1 and 65535"
                raise ValueError(msg)
        return value

    @property
    def has_checkpoint(self) -> bool:
        return bool(self.checkpoint_exposed_ports)


class StartupPortBindingPlan(ContractModel):
    request: ContainerStartupPortRequest
    bind_ports: list[int]
    bindings: list[PortBinding] = Field(default_factory=list)
    exposed_ports: list[int] = Field(default_factory=list)


class ResolvConfDecision(ContractModel):
    source: str
    using_host: bool
    reason: str


class RuntimeStartedWaitPlan(ContractModel):
    action: RuntimeStartedWaitAction
    reason: RuntimeStartedWaitReason
    pid: int | None = None

    @property
    def should_handle_pid(self) -> bool:
        return self.action is RuntimeStartedWaitAction.HandlePid and self.pid is not None


class AppliedContainerResources(ContractModel):
    cpu: OciLinuxCpu | None = None
    memory: OciLinuxMemory | None = None
    unified: dict[str, str] = Field(default_factory=dict)

    @property
    def has_cpu(self) -> bool:
        return self.cpu is not None

    @property
    def has_memory(self) -> bool:
        return self.memory is not None


class ContainerResourceApplicationPlan(ContractModel):
    resources: AppliedContainerResources
    deferred_cpu: OciLinuxCpu | None = None
    cpu_enforced: bool = False
    memory_enforced: bool = False
    sandbox_cpu_deferred: bool = False


class DeferredCpuApplyPlan(ContractModel):
    action: DeferredCpuApplyAction
    resources: AppliedContainerResources | None = None
    clear_deferred_cpu: bool = False
    error_message: str = ""


class StubEntrypointConfig(ContractModel):
    python_executable: str = DEFAULT_WORKER_PYTHON_EXECUTABLE
    entrypoint: list[str] = Field(default_factory=list)
    valid: bool = True


class FallbackEntrypointRequest(ContractModel):
    kind: WorkerStartupKind
    env: list[str] = Field(default_factory=list)
    stub_config: StubEntrypointConfig | None = None
    config: JsonObject = Field(default_factory=dict)


class FallbackEntrypointPlan(ContractModel):
    entrypoint: list[str] = Field(default_factory=list)
    reason: FallbackEntrypointReason
    python_executable: str = DEFAULT_WORKER_PYTHON_EXECUTABLE
    runner_module: str = ""
    ok: bool = False


class RequestMountLinkPlan(ContractModel):
    target: str
    link_path: str
    replace_existing: bool = True


class PreparedRequestMount(ContractModel):
    mount: RequestMount
    action: RequestMountPrepareAction
    reason: RequestMountPrepareReason
    oci_mount: OciMount | None = None
    create_source_dir: bool = False
    link: RequestMountLinkPlan | None = None
    volume_cache_key: str = ""

    @property
    def included(self) -> bool:
        return self.action is RequestMountPrepareAction.Mount


class RequestMountPlan(ContractModel):
    mounts: list[PreparedRequestMount]
    oci_mounts: list[OciMount]
    volume_cache_map: dict[str, str] = Field(default_factory=dict)
    source_dirs_to_create: list[str] = Field(default_factory=list)
    symlinks: list[RequestMountLinkPlan] = Field(default_factory=list)


class RequestMountSetupPlan(ContractModel):
    container_id: str
    workspace_name: str
    mounts: list[RequestMount]
    workspace_storage_available: bool = False
    workspace_storage_base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH
    mountpoint_mounts: list[RequestMount] = Field(default_factory=list)


class BindMountSourceDirPlan(ContractModel):
    mount_path: str
    local_path: str
    action: BindMountSourceDirAction
    reason: BindMountSourceDirReason


def resolv_conf_has_usable_nameserver(text: str) -> bool:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] != "nameserver":
            continue
        try:
            ip = ipaddress.ip_address(fields[1])
        except ValueError:
            continue
        if not ip.is_loopback and not ip.is_unspecified:
            return True
    return False


def select_container_resolv_conf_source(
    *,
    use_host_resolv_conf: bool,
    host_path: str = HOST_RESOLV_CONF_PATH,
    fallback_path: str = WORKER_RESOLV_CONF_PATH,
    host_resolv_content: str | None = None,
) -> ResolvConfDecision:
    if not use_host_resolv_conf:
        return ResolvConfDecision(
            source=fallback_path,
            using_host=False,
            reason="host resolver disabled",
        )
    content = host_resolv_content
    if content is None:
        try:
            content = Path(host_path).read_text(encoding="utf-8")
        except OSError:
            content = ""
    if resolv_conf_has_usable_nameserver(content):
        return ResolvConfDecision(
            source=host_path,
            using_host=True,
            reason="host resolver has a reachable nameserver",
        )
    return ResolvConfDecision(
        source=fallback_path,
        using_host=False,
        reason="host resolver has no usable nameserver",
    )


def required_container_resolv_conf_source(
    *,
    host_path: str = HOST_RESOLV_CONF_PATH,
    fallback_path: str = WORKER_RESOLV_CONF_PATH,
) -> Path:
    decision = select_container_resolv_conf_source(
        use_host_resolv_conf=True,
        host_path=host_path,
        fallback_path=fallback_path,
    )
    source = Path(decision.source)
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"worker resolver source is unavailable: {source}") from exc
    if not resolv_conf_has_usable_nameserver(content):
        raise RuntimeError(f"worker resolver source has no usable nameserver: {source}")
    return source


def startup_container_ports(request: ContainerStartupPortRequest) -> list[int]:
    if request.checkpoint_exposed_ports:
        return list(request.checkpoint_exposed_ports)
    ports = list(request.ports) or [CONTAINER_INNER_PORT]
    ports.append(WORKER_SHELL_PORT)
    if request.kind is WorkerStartupKind.Sandbox:
        ports.append(WORKER_SANDBOX_PROCESS_MANAGER_PORT)
    return ports


def plan_startup_port_bindings(
    request: ContainerStartupPortRequest,
    *,
    bind_ports: list[int],
) -> StartupPortBindingPlan:
    if not request.ports or not bind_ports:
        return StartupPortBindingPlan(request=request, bind_ports=list(bind_ports))

    if request.has_checkpoint:
        exposed = set(request.ports)
    elif request.kind is WorkerStartupKind.Sandbox:
        exposed = set(request.requested_ports)
    else:
        exposed = set(request.ports)

    bindings: list[PortBinding] = []
    for index, container_port in enumerate(request.ports):
        if index >= len(bind_ports):
            break
        if container_port not in exposed:
            continue
        bindings.append(PortBinding(host_port=bind_ports[index], container_port=container_port))
    return StartupPortBindingPlan(
        request=request,
        bind_ports=list(bind_ports),
        bindings=bindings,
        exposed_ports=sorted(exposed),
    )


def plan_runtime_started_wait(
    *,
    runtime_started_pid: int | None = None,
    runtime_done: bool = False,
    runtime_done_pid: int | None = None,
    context_done: bool = False,
) -> RuntimeStartedWaitPlan:
    if runtime_started_pid is not None:
        return RuntimeStartedWaitPlan(
            action=RuntimeStartedWaitAction.HandlePid,
            reason=RuntimeStartedWaitReason.RuntimeStarted,
            pid=runtime_started_pid,
        )
    if context_done:
        return RuntimeStartedWaitPlan(
            action=RuntimeStartedWaitAction.Return,
            reason=RuntimeStartedWaitReason.ContextDone,
        )
    if runtime_done:
        if runtime_done_pid is not None:
            return RuntimeStartedWaitPlan(
                action=RuntimeStartedWaitAction.HandlePid,
                reason=RuntimeStartedWaitReason.RuntimeDoneDrainedPid,
                pid=runtime_done_pid,
            )
        return RuntimeStartedWaitPlan(
            action=RuntimeStartedWaitAction.Return,
            reason=RuntimeStartedWaitReason.RuntimeDoneNoPid,
        )
    return RuntimeStartedWaitPlan(
        action=RuntimeStartedWaitAction.Await,
        reason=RuntimeStartedWaitReason.Waiting,
    )


def plan_container_resource_application(
    resources: OciLinuxResources,
    *,
    cpu_enforced: bool,
    memory_enforced: bool,
    sandbox: bool = False,
    runtime_can_update_resources: bool = False,
) -> ContainerResourceApplicationPlan:
    applied_cpu = resources.cpu if cpu_enforced else None
    deferred_cpu: OciLinuxCpu | None = None
    sandbox_cpu_deferred = False
    if applied_cpu is not None and sandbox and runtime_can_update_resources:
        deferred_cpu = applied_cpu
        applied_cpu = None
        sandbox_cpu_deferred = True

    applied_memory = resources.memory if memory_enforced else None
    return ContainerResourceApplicationPlan(
        resources=AppliedContainerResources(
            cpu=applied_cpu,
            memory=applied_memory,
            unified=dict(resources.unified) if memory_enforced else {},
        ),
        deferred_cpu=deferred_cpu,
        cpu_enforced=cpu_enforced,
        memory_enforced=memory_enforced,
        sandbox_cpu_deferred=sandbox_cpu_deferred,
    )


def plan_deferred_sandbox_cpu_apply(
    deferred_cpu: OciLinuxCpu | None,
    *,
    runtime_name: str = "",
    runtime_can_update_resources: bool,
    update_error: str = "",
) -> DeferredCpuApplyPlan:
    if deferred_cpu is None:
        return DeferredCpuApplyPlan(action=DeferredCpuApplyAction.Noop)
    if not runtime_can_update_resources:
        error = (
            "runtime is nil"
            if not runtime_name
            else f"runtime {runtime_name} does not support resource updates"
        )
        return DeferredCpuApplyPlan(
            action=DeferredCpuApplyAction.Reject,
            error_message=error,
        )
    resources = AppliedContainerResources(cpu=deferred_cpu)
    if update_error:
        return DeferredCpuApplyPlan(
            action=DeferredCpuApplyAction.Apply,
            resources=resources,
            clear_deferred_cpu=False,
            error_message=update_error,
        )
    return DeferredCpuApplyPlan(
        action=DeferredCpuApplyAction.Apply,
        resources=resources,
        clear_deferred_cpu=True,
    )


def env_has_prefix(env: list[str], prefix: str) -> bool:
    return any(item.startswith(prefix) for item in env)


def parse_stub_entrypoint_config(raw: str) -> StubEntrypointConfig | None:
    if not raw.strip():
        return None
    try:
        payload = _JSON_OBJECT.validate_json(raw)
    except ValidationError:
        return StubEntrypointConfig(valid=False)
    raw_entrypoint = payload.get("entry_point") or payload.get("entrypoint") or []
    entrypoint = (
        [item for item in raw_entrypoint if isinstance(item, str)]
        if isinstance(raw_entrypoint, list)
        else []
    )
    python_executable = payload.get("python_version") or payload.get("python_executable")
    if not isinstance(python_executable, str) or not python_executable:
        python_executable = DEFAULT_WORKER_PYTHON_EXECUTABLE
    return StubEntrypointConfig(
        python_executable=python_executable,
        entrypoint=entrypoint,
        valid=True,
    )


def fallback_runner_module(kind: WorkerStartupKind) -> str:
    if kind in {WorkerStartupKind.Endpoint, WorkerStartupKind.Asgi}:
        return ENDPOINT_RUNNER_MODULE
    if kind is WorkerStartupKind.Function:
        return FUNCTION_RUNNER_MODULE
    return ""


def plan_fallback_entrypoint(request: FallbackEntrypointRequest) -> FallbackEntrypointPlan:
    config = request.stub_config
    if config is None and request.config:
        config = StubEntrypointConfig.model_validate(request.config)
    if config is not None and not config.valid:
        return FallbackEntrypointPlan(
            reason=FallbackEntrypointReason.InvalidConfig,
            python_executable=DEFAULT_WORKER_PYTHON_EXECUTABLE,
        )
    if config is not None and config.entrypoint:
        return FallbackEntrypointPlan(
            entrypoint=list(config.entrypoint),
            reason=FallbackEntrypointReason.ExplicitConfig,
            python_executable=config.python_executable,
            ok=True,
        )

    runner_module = fallback_runner_module(request.kind)
    python_executable = (
        config.python_executable if config is not None else DEFAULT_WORKER_PYTHON_EXECUTABLE
    )
    if not runner_module:
        return FallbackEntrypointPlan(
            reason=FallbackEntrypointReason.UnsupportedKind,
            python_executable=python_executable,
        )
    if not env_has_prefix(request.env, HANDLER_ENV_PREFIX):
        return FallbackEntrypointPlan(
            reason=FallbackEntrypointReason.MissingHandler,
            python_executable=python_executable,
            runner_module=runner_module,
        )
    return FallbackEntrypointPlan(
        entrypoint=[python_executable, "-m", runner_module],
        reason=FallbackEntrypointReason.Runner,
        python_executable=python_executable,
        runner_module=runner_module,
        ok=True,
    )


def bind_mount_mode(mount: RequestMount) -> str:
    return "ro" if mount.read_only else "rw"


def request_mount_oci_mount(mount: RequestMount) -> OciMount:
    return OciMount(
        mount_type=OciMountType.NoneMount,
        source=mount.local_path,
        destination=mount.mount_path,
        options=["rbind", bind_mount_mode(mount)],
    )


def request_mount_is_workspace_volume(
    mount_path: str,
    *,
    container_volume_path: str = WORKER_CONTAINER_VOLUME_PATH,
) -> bool:
    root = container_volume_path.rstrip("/")
    return mount_path == root or mount_path.startswith(root + "/")


def request_volume_cache_key(
    mount_path: str,
    *,
    container_volume_path: str = WORKER_CONTAINER_VOLUME_PATH,
) -> str:
    if not request_mount_is_workspace_volume(
        mount_path,
        container_volume_path=container_volume_path,
    ):
        return ""
    key = posixpath.basename(posixpath.normpath(mount_path))
    return "" if key in {"", "."} else key


def workspace_storage_mount_path(
    workspace_name: str,
    prefix: str,
    *,
    base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
) -> str:
    return posixpath.join(base_mount_path.rstrip("/"), workspace_name, prefix)


def rewrite_workspace_storage_local_path(
    local_path: str,
    *,
    workspace_name: str,
    source_root: str,
    target_prefix: str,
    base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
) -> str:
    source = posixpath.join(source_root.rstrip("/"), workspace_name)
    target = workspace_storage_mount_path(
        workspace_name,
        target_prefix,
        base_mount_path=base_mount_path,
    )
    if local_path == source:
        return target
    if local_path.startswith(source + "/"):
        return target + local_path.removeprefix(source)
    return local_path


def adjust_mount_for_workspace_storage(
    mount: RequestMount,
    *,
    workspace_name: str,
    base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
) -> RequestMount:
    if mount.mount_type is RequestMountType.MountPoint:
        return mount
    if mount.mount_type is RequestMountType.Volume:
        # A platform volume lives in the workspace's own storage, so it resolves
        # the same way outputs do. Without this the mount keeps the logical
        # /data path, which nothing backs, and writes land on local disk.
        return mount.model_copy(
            update={
                "local_path": rewrite_workspace_storage_local_path(
                    mount.local_path,
                    workspace_name=workspace_name,
                    source_root=DEFAULT_VOLUMES_PATH,
                    target_prefix=DEFAULT_VOLUMES_PREFIX,
                    base_mount_path=base_mount_path,
                )
            }
        )
    mount_path = mount.mount_path.rstrip("/")
    if mount_path != WORKER_USER_ARTIFACT_VOLUME and not mount_path.startswith(
        WORKER_USER_ARTIFACT_VOLUME + "/"
    ):
        return mount
    return mount.model_copy(
        update={
            "local_path": rewrite_workspace_storage_local_path(
                mount.local_path,
                workspace_name=workspace_name,
                source_root=DEFAULT_ARTIFACTS_PATH,
                target_prefix=DEFAULT_ARTIFACTS_PREFIX,
                base_mount_path=base_mount_path,
            )
        }
    )


def setup_mountpoint_local_path(mount: RequestMount, *, container_id: str) -> RequestMount:
    if mount.mount_type is not RequestMountType.MountPoint or mount.mountpoint_config is None:
        return mount
    local_path = posixpath.join(
        mount.local_path,
        container_id,
        mount.mountpoint_config.bucket_name,
    )
    return mount.model_copy(update={"local_path": local_path})


def plan_request_mount_setup(
    mounts: list[RequestMount],
    *,
    container_id: str,
    workspace_name: str,
    workspace_storage_available: bool,
    workspace_storage_base_mount_path: str = DEFAULT_WORKSPACE_STORAGE_BASE_MOUNT_PATH,
) -> RequestMountSetupPlan:
    planned: list[RequestMount] = []
    mountpoint_mounts: list[RequestMount] = []
    for mount in mounts:
        current = mount
        if workspace_storage_available:
            current = adjust_mount_for_workspace_storage(
                current,
                workspace_name=workspace_name,
                base_mount_path=workspace_storage_base_mount_path,
            )
        current = setup_mountpoint_local_path(current, container_id=container_id)
        planned.append(current)
        if current.mount_type is RequestMountType.MountPoint:
            mountpoint_mounts.append(current)
    return RequestMountSetupPlan(
        container_id=container_id,
        workspace_name=workspace_name,
        mounts=planned,
        workspace_storage_available=workspace_storage_available,
        workspace_storage_base_mount_path=workspace_storage_base_mount_path,
        mountpoint_mounts=mountpoint_mounts,
    )


def plan_request_mount_preparation(
    mount: RequestMount,
    *,
    mountpoint_exists: bool = False,
) -> PreparedRequestMount:
    if mount.mount_type is RequestMountType.MountPoint:
        if not mountpoint_exists:
            return PreparedRequestMount(
                mount=mount,
                action=RequestMountPrepareAction.Skip,
                reason=RequestMountPrepareReason.MountPointMissing,
            )
        return PreparedRequestMount(
            mount=mount,
            action=RequestMountPrepareAction.Mount,
            reason=RequestMountPrepareReason.MountPointExists,
            oci_mount=request_mount_oci_mount(mount),
            link=_request_mount_link_plan(mount),
        )

    if not mount.local_path:
        return PreparedRequestMount(
            mount=mount,
            action=RequestMountPrepareAction.Skip,
            reason=RequestMountPrepareReason.EmptyLocalPath,
        )

    cache_key = request_volume_cache_key(mount.mount_path)
    return PreparedRequestMount(
        mount=mount,
        action=RequestMountPrepareAction.Mount,
        reason=RequestMountPrepareReason.BindSourceReady,
        oci_mount=request_mount_oci_mount(mount),
        create_source_dir=True,
        link=_request_mount_link_plan(mount),
        volume_cache_key=cache_key,
    )


def plan_request_mounts(
    mounts: list[RequestMount],
    *,
    existing_mountpoint_paths: set[str] | None = None,
) -> RequestMountPlan:
    existing_paths = existing_mountpoint_paths
    if existing_paths is None:
        existing_paths = {mount.local_path for mount in mounts if Path(mount.local_path).exists()}
    prepared: list[PreparedRequestMount] = []
    oci_mounts: list[OciMount] = []
    volume_cache_map: dict[str, str] = {}
    source_dirs_to_create: list[str] = []
    symlinks: list[RequestMountLinkPlan] = []
    for mount in mounts:
        item = plan_request_mount_preparation(
            mount,
            mountpoint_exists=mount.local_path in existing_paths,
        )
        prepared.append(item)
        if not item.included:
            continue
        if item.oci_mount is not None:
            oci_mounts.append(item.oci_mount)
        if item.create_source_dir:
            source_dirs_to_create.append(item.mount.local_path)
        if item.link is not None:
            symlinks.append(item.link)
        if item.volume_cache_key:
            volume_cache_map[item.volume_cache_key] = item.mount.local_path
    return RequestMountPlan(
        mounts=prepared,
        oci_mounts=oci_mounts,
        volume_cache_map=volume_cache_map,
        source_dirs_to_create=source_dirs_to_create,
        symlinks=symlinks,
    )


def plan_bind_mount_source_dirs(mounts: list[RequestMount]) -> list[BindMountSourceDirPlan]:
    plans: list[BindMountSourceDirPlan] = []
    for mount in mounts:
        if mount.mount_type is RequestMountType.MountPoint:
            plans.append(
                BindMountSourceDirPlan(
                    mount_path=mount.mount_path,
                    local_path=mount.local_path,
                    action=BindMountSourceDirAction.Skip,
                    reason=BindMountSourceDirReason.MountPoint,
                )
            )
            continue
        if not mount.local_path:
            plans.append(
                BindMountSourceDirPlan(
                    mount_path=mount.mount_path,
                    local_path=mount.local_path,
                    action=BindMountSourceDirAction.Skip,
                    reason=BindMountSourceDirReason.EmptyLocalPath,
                )
            )
            continue
        plans.append(
            BindMountSourceDirPlan(
                mount_path=mount.mount_path,
                local_path=mount.local_path,
                action=BindMountSourceDirAction.Create,
                reason=BindMountSourceDirReason.BindMountSource,
            )
        )
    return plans


def ensure_bind_mount_source_dirs(mounts: list[RequestMount]) -> list[BindMountSourceDirPlan]:
    plans = plan_bind_mount_source_dirs(mounts)
    for plan in plans:
        if plan.action is BindMountSourceDirAction.Create:
            Path(plan.local_path).mkdir(parents=True, exist_ok=True)
    return plans


def _request_mount_link_plan(mount: RequestMount) -> RequestMountLinkPlan | None:
    if not mount.link_path:
        return None
    return RequestMountLinkPlan(target=mount.mount_path, link_path=mount.link_path)

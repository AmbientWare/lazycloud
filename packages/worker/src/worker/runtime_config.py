from __future__ import annotations

import posixpath
import shutil
from collections.abc import Callable
from copy import deepcopy
from enum import StrEnum
from pathlib import Path

from pydantic import Field, JsonValue, TypeAdapter, field_validator
from shared.app_identity import CLI_NAME
from shared.container_requests import OciRuntimeName, RuntimeContainerStatus
from shared.contracts import ContractModel

DEFAULT_RUNSC_ROOT = "/run/gvisor"
DEFAULT_GVISOR_OOM_THRESHOLD_PERCENT = 95.0
DEFAULT_OCI_NAMESPACES = ("mount", "pid", "ipc", "uts", "cgroup")
DEFAULT_CONTAINER_CLI_SOURCE = f"/app/.venv/bin/{CLI_NAME}"
DEFAULT_CONTAINER_CLI_PATH = f"/usr/bin/{CLI_NAME}"
# Sized from the container's memory request by the spec builder. The fallback
# applies only where no request is known, and is deliberately small.
DEFAULT_CONTAINER_TMPFS_SIZE_MIB = 64

type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]

_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_JSON_ARRAY_ADAPTER: TypeAdapter[JsonArray] = TypeAdapter(JsonArray)


class RuntimeEngine(StrEnum):
    LocalProcess = "local-process"
    Oci = "oci"
    SandboxedOci = "sandboxed-oci"


class RuntimeOperation(StrEnum):
    Run = "run"
    Exec = "exec"
    Kill = "kill"
    Delete = "delete"
    State = "state"
    List = "list"
    Checkpoint = "checkpoint"
    Restore = "restore"


class RuntimeEventType(StrEnum):
    Oom = "oom"
    Exit = "exit"
    Error = "error"


class RuntimeAvailabilityStatus(StrEnum):
    Available = "available"
    Missing = "missing"
    ProbeFailed = "probe-failed"


class OomWatcherKind(StrEnum):
    Cgroup = "cgroup"
    ProcessMemory = "process-memory"


class RuntimeConfig(ContractModel):
    engine: RuntimeEngine = RuntimeEngine.LocalProcess
    rootfs: str | None = None
    working_directory: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    mounts: dict[str, str] = Field(default_factory=dict)
    network_enabled: bool = True
    readonly_rootfs: bool = False


class RuntimeCapabilities(ContractModel):
    checkpoint_restore: bool
    gpu: bool
    oom_events: bool
    join_existing_netns: bool
    cdi: bool


class RuntimeBinaryConfig(ContractModel):
    runtime: OciRuntimeName = OciRuntimeName.Runsc
    runsc_path: str = "runsc"
    runsc_platform: str = ""
    runsc_root: str = DEFAULT_RUNSC_ROOT
    runsc_extra_args: list[str] = Field(default_factory=list)
    debug: bool = False

    @field_validator("runsc_path", "runsc_root")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "runtime binary settings cannot be blank"
            raise ValueError(msg)
        return stripped

    @property
    def binary_path(self) -> str:
        return self.runsc_path


class RuntimeAvailability(ContractModel):
    runtime: OciRuntimeName
    status: RuntimeAvailabilityStatus
    binary_path: str
    resolved_path: str | None = None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status is RuntimeAvailabilityStatus.Available


class RuntimeUnavailableError(RuntimeError):
    def __init__(self, availability: RuntimeAvailability) -> None:
        self.availability = availability
        detail = availability.reason or "runtime capability probe failed"
        super().__init__(f"runtime {availability.runtime.value} is unavailable: {detail}")


class RuntimeState(ContractModel):
    id: str
    pid: int = 0
    status: RuntimeContainerStatus = RuntimeContainerStatus.Unknown
    bundle: str | None = None

    @field_validator("pid")
    @classmethod
    def pid_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "runtime state pid cannot be negative"
            raise ValueError(msg)
        return value


class RuntimeEvent(ContractModel):
    event_type: RuntimeEventType
    error: str | None = None


class RuntimeCommandRequest(ContractModel):
    operation: RuntimeOperation
    container_id: str | None = None
    bundle_path: str | None = None
    process_spec_path: str | None = None
    signal: int = 15
    force: bool = False
    all_processes: bool = False
    docker_enabled: bool = False
    nvproxy: bool = False
    image_path: str | None = None
    work_dir: str | None = None
    leave_running: bool = False
    allow_open_tcp: bool = False
    skip_in_flight: bool = False
    link_remap: bool = False
    tcp_close: bool = False

    @field_validator("signal")
    @classmethod
    def signal_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "signal must be positive"
            raise ValueError(msg)
        return value


class RuntimeCommandPlan(ContractModel):
    runtime: OciRuntimeName
    operation: RuntimeOperation
    argv: list[str]
    uses_process_group: bool = False
    cleanup_argv: list[str] | None = None


class OciSpecPreparation(ContractModel):
    runtime: OciRuntimeName
    spec: JsonObject
    gpu_detected: bool = False
    nvproxy_enabled: bool = False
    added_mounts: list[JsonObject] = Field(default_factory=list)
    added_capabilities: list[str] = Field(default_factory=list)


class OomCounterSnapshot(ContractModel):
    oom_kill: int = 0
    under_oom: int = 0


class OomDecision(ContractModel):
    watcher: OomWatcherKind
    triggered: bool
    reason: str
    usage_percent: float = 0.0


type WhichResolver = Callable[[str], str | None]
type RuntimeBinaryVerifier = Callable[[str], str | None]


def base_runtime_config(engine: RuntimeEngine = RuntimeEngine.LocalProcess) -> RuntimeConfig:
    return RuntimeConfig(engine=engine)


def normalize_oci_runtime(value: OciRuntimeName | RuntimeEngine | str) -> OciRuntimeName:
    raw = value.value if isinstance(value, StrEnum) else str(value)
    normalized = raw.strip().lower()
    # An unspecified runtime sandboxes. This is the worker's own normaliser, so a
    # default of runc here would quietly undo the sandbox for any path that did
    # not name a runtime explicitly.
    if normalized in {RuntimeEngine.Oci.value, ""}:
        return OciRuntimeName.Runsc
    if normalized == OciRuntimeName.Runc.value:
        # A bundle or spec written before the move to gVisor names runc. Honour
        # the container, not the obsolete runtime: recovering an in-flight
        # workload must not be the one path that escapes the sandbox.
        return OciRuntimeName.Runsc
    if normalized in {
        OciRuntimeName.Runsc.value,
        RuntimeEngine.SandboxedOci.value,
        "gvisor",
    }:
        return OciRuntimeName.Runsc
    msg = f"unsupported OCI runtime: {raw}"
    raise ValueError(msg)


def runtime_capabilities(runtime: OciRuntimeName | RuntimeEngine | str) -> RuntimeCapabilities:
    normalize_oci_runtime(runtime)
    # gVisor reports OOM kills through its own sentry rather than the host cgroup
    # event file the worker watches, so oom_events stays false.
    return RuntimeCapabilities(
        checkpoint_restore=True,
        gpu=True,
        oom_events=False,
        join_existing_netns=True,
        cdi=True,
    )


def runtime_availability(
    config: RuntimeBinaryConfig,
    *,
    which: WhichResolver = shutil.which,
    verify: RuntimeBinaryVerifier | None = None,
) -> RuntimeAvailability:
    resolved = which(config.binary_path)
    if resolved is None:
        return RuntimeAvailability(
            runtime=config.runtime,
            status=RuntimeAvailabilityStatus.Missing,
            binary_path=config.binary_path,
            reason=f"{config.binary_path} binary not found",
        )
    if verify is not None:
        reason = verify(resolved)
        if reason is not None:
            return RuntimeAvailability(
                runtime=config.runtime,
                status=RuntimeAvailabilityStatus.ProbeFailed,
                binary_path=config.binary_path,
                resolved_path=resolved,
                reason=reason,
            )
    return RuntimeAvailability(
        runtime=config.runtime,
        status=RuntimeAvailabilityStatus.Available,
        binary_path=config.binary_path,
        resolved_path=resolved,
    )


def build_base_oci_config(
    runtime: OciRuntimeName | RuntimeEngine | str = OciRuntimeName.Runsc,
    *,
    root_path: str = "rootfs",
    command: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str = "/",
    hostname: str | None = None,
    readonly_rootfs: bool = True,
    container_cli_source: str | None = None,
    container_cli_path: str = DEFAULT_CONTAINER_CLI_PATH,
    tmpfs_size_mib: int = DEFAULT_CONTAINER_TMPFS_SIZE_MIB,
) -> JsonObject:
    selected = normalize_oci_runtime(runtime)
    capabilities = _base_capabilities()
    process_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TERM": "xterm",
        "TASK_ROOT": "/workspace",
        "MAMBA_ROOT_PREFIX": "/micromamba",
        **(env or {}),
    }
    process_args: JsonArray = (
        list(command)
        if command is not None
        else [
            "tail",
            "-f",
            "/dev/null",
        ]
    )
    process_env_values: JsonArray = [f"{key}={value}" for key, value in sorted(process_env.items())]
    mounts: JsonArray = [
        _mount("/proc", "proc", "proc", ["rw", "nosuid", "noexec", "nodev"]),
        # tmpfs pages are charged to the container's memory cgroup, so an
        # unsized tmpfs lets a container consume its whole memory ceiling here
        # and, with no ceiling at all, the worker's RAM.
        _mount(
            "/volumes",
            "tmpfs",
            "tmpfs",
            ["nosuid", "strictatime", "mode=755", f"size={tmpfs_size_mib}m"],
        ),
        _mount(
            "/dev",
            "tmpfs",
            "tmpfs",
            ["rw", "nosuid", "strictatime", "mode=755", "size=65536k"],
        ),
        _mount("/dev/pts", "devpts", "devpts", ["nosuid", "noexec", "newinstance"]),
        _mount(
            "/dev/shm",
            "tmpfs",
            "shm",
            ["nosuid", "noexec", "nodev", "mode=1777", f"size={tmpfs_size_mib}m"],
        ),
        _mount("/dev/mqueue", "mqueue", "mqueue", ["nosuid", "noexec", "nodev"]),
        _mount("/sys", "sysfs", "sysfs", ["rw", "nosuid", "noexec", "nodev"]),
    ]
    if container_cli_source:
        mounts.append(
            _mount(
                container_cli_path,
                "bind",
                container_cli_source,
                ["ro", "rbind", "rprivate", "nosuid", "nodev"],
            )
        )
    user: JsonObject = {"uid": 0, "gid": 0}
    rlimits: JsonArray = []
    process: JsonObject = {
        "terminal": False,
        "user": user,
        "args": process_args,
        "env": process_env_values,
        "cwd": cwd,
        "capabilities": capabilities,
        "rlimits": rlimits,
        "noNewPrivileges": False,
    }
    root: JsonObject = {"path": root_path, "readonly": readonly_rootfs}
    resources: JsonObject = {}
    devices: JsonArray = []
    namespaces: JsonArray = [_namespace(namespace) for namespace in DEFAULT_OCI_NAMESPACES]
    linux: JsonObject = {
        "resources": resources,
        "devices": devices,
        "namespaces": namespaces,
    }
    annotations: JsonObject = {}
    config: JsonObject = {
        "ociVersion": "1.1.0",
        "process": process,
        "root": root,
        "hostname": hostname or selected.value,
        "mounts": mounts,
        "linux": linux,
        "annotations": annotations,
    }
    return config


def plan_runtime_command(
    config: RuntimeBinaryConfig,
    request: RuntimeCommandRequest,
) -> RuntimeCommandPlan:
    return _plan_runsc_command(config, request)


def prepare_oci_spec_for_runtime(
    spec: JsonObject,
    runtime: OciRuntimeName | RuntimeEngine | str,
    *,
    cuda_checkpoint_path: str | None = None,
    docker_enabled: bool = False,
) -> OciSpecPreparation:
    selected = normalize_oci_runtime(runtime)
    prepared = deepcopy(spec)
    gpu_detected = spec_has_gpu(prepared)
    added_mounts: list[JsonObject] = []
    added_capabilities: list[str] = []
    if selected is OciRuntimeName.Runsc:
        linux = _ensure_dict(prepared, "linux")
        linux.pop("seccomp", None)
        # The nvidia device nodes must survive. Clearing them for a sandbox looks
        # right and is not: gVisor decides a container wants a GPU by finding
        # /dev/nvidiactl among linux.devices, or by an nvidia hook it can read
        # NVIDIA_VISIBLE_DEVICES from, and hooks are stripped here. Emptying the
        # list left --nvproxy set over a sandbox gVisor had concluded needed no
        # GPU. It removes the frontend nodes itself and serves them from the
        # sentry, so passing them through does not expose the host devices.
        if gpu_detected and cuda_checkpoint_path:
            mount = _mount(
                "/usr/local/bin/cuda-checkpoint",
                "bind",
                cuda_checkpoint_path,
                ["bind", "ro"],
            )
            _ensure_list(prepared, "mounts").append(mount)
            added_mounts.append(mount)
        if docker_enabled:
            added_capabilities = add_docker_in_docker_capabilities(prepared)
    return OciSpecPreparation(
        runtime=selected,
        spec=prepared,
        gpu_detected=gpu_detected,
        nvproxy_enabled=selected is OciRuntimeName.Runsc and gpu_detected,
        added_mounts=added_mounts,
        added_capabilities=added_capabilities,
    )


def spec_has_gpu(spec: JsonObject) -> bool:
    linux = spec.get("linux")
    if isinstance(linux, dict):
        devices = linux.get("devices")
        if isinstance(devices, list):
            for device in devices:
                if isinstance(device, dict) and str(device.get("path", "")).startswith(
                    "/dev/nvidia"
                ):
                    return True
    annotations = spec.get("annotations")
    if isinstance(annotations, dict):
        return any(str(key).startswith("cdi.k8s.io") for key in annotations)
    return False


def add_docker_in_docker_capabilities(spec: JsonObject) -> list[str]:
    process = _ensure_dict(spec, "process")
    capabilities = _ensure_dict(process, "capabilities")
    added: list[str] = []
    for field_name in ["bounding", "effective", "permitted", "inheritable"]:
        existing = capabilities.get(field_name)
        values = [str(item) for item in existing] if isinstance(existing, list) else []
        merged = _merge_unique(values, DOCKER_IN_DOCKER_CAPABILITIES)
        capabilities[field_name] = _json_string_list(merged)
        added.extend(item for item in merged if item not in values)
    return sorted(set(added))


def parse_runtime_state(payload: str | bytes | JsonObject) -> RuntimeState:
    raw = _decode_json_object(payload)
    status = _parse_status(raw.get("status"))
    pid = _parse_int(raw.get("pid"), field_name="pid")
    if status is RuntimeContainerStatus.Stopped and pid == -1:
        pid = 0
    return RuntimeState(
        id=str(raw.get("id", "")),
        pid=pid,
        status=status,
        bundle=str(raw["bundle"]) if raw.get("bundle") is not None else None,
    )


def parse_runtime_list(payload: str | bytes | JsonArray) -> list[RuntimeState]:
    raw = (
        _JSON_ARRAY_ADAPTER.validate_json(payload)
        if isinstance(payload, str | bytes)
        else _JSON_ARRAY_ADAPTER.validate_python(payload)
    )
    return [parse_runtime_state(item) for item in raw if isinstance(item, dict)]


def parse_oom_counter_snapshot(text: str) -> OomCounterSnapshot:
    counts = {"oom_kill": 0, "under_oom": 0}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        key = parts[0]
        if key in counts:
            try:
                counts[key] = int(parts[1])
            except ValueError as exc:
                msg = f"invalid OOM counter value for {key}: {parts[1]}"
                raise ValueError(msg) from exc
    return OomCounterSnapshot(**counts)


def cgroup_oom_decision(previous: OomCounterSnapshot, current: OomCounterSnapshot) -> OomDecision:
    triggered = current.oom_kill > previous.oom_kill or current.under_oom > previous.under_oom
    return OomDecision(
        watcher=OomWatcherKind.Cgroup,
        triggered=triggered,
        reason="oom counter increased" if triggered else "oom counters unchanged",
    )


MEMINFO_PATH = "/proc/meminfo"


def read_machine_memory_mib(*, path: str = MEMINFO_PATH) -> int:
    """What this machine holds, or zero when it cannot be read.

    Zero rather than a guess. It bounds a container's hard ceiling, so an
    unreadable machine costs a ceiling that may be too generous, where an
    invented one costs containers that will not start on a machine that could
    have run them.
    """
    try:
        return parse_meminfo_total_mib(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0


def read_memory_pressure_percent(cgroup_path: str) -> float:
    """Full memory stall over the last ten seconds, or zero when unreadable.

    Zero reads as "coping", which is the safe direction: a pressure file this
    worker cannot read must not evict anybody.
    """
    try:
        return parse_memory_pressure_percent(
            Path(cgroup_path, "memory.pressure").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return 0.0


def read_container_memory_current(cgroup_path: str) -> int:
    """Bytes this container currently holds, or zero when unreadable."""
    try:
        return int(Path(cgroup_path, "memory.current").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def parse_meminfo_total_mib(text: str) -> int:
    """What the machine holds, from `/proc/meminfo`.

    Read rather than configured because it is a fact about the host the worker is
    already running on, and a configured figure is one that can be wrong on a
    machine nobody re-configured after resizing it.
    """
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name.strip() != "MemTotal":
            continue
        fields = rest.split()
        if not fields:
            break
        return int(fields[0]) // 1024
    msg = "MemTotal not found in meminfo"
    raise ValueError(msg)


def parse_memory_pressure_percent(text: str) -> float:
    """How much of the last ten seconds every task spent stalled on memory.

    The `full` line, not `some`: `some` counts a window where any task waited,
    which a healthy machine does constantly. `full` counts windows where nothing
    could run at all, which is the machine having stopped rather than slowed.
    """
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "full":
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if key == "avg10":
                return float(value)
    msg = "no full avg10 in pressure file"
    raise ValueError(msg)


def parse_proc_cgroup_path(text: str) -> str:
    for line in text.splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy_id, controllers, cgroup_path = parts
        clean_path = cgroup_path.strip().lstrip("/")
        if hierarchy_id == "0" and clean_path:
            return clean_path
        if "memory" in controllers.split(",") and clean_path:
            return posixpath.join("memory", clean_path)
    msg = "cgroup path not found"
    raise ValueError(msg)


def process_memory_oom_decision(
    *,
    memory_usage_bytes: int,
    memory_limit_bytes: int,
    threshold_percent: float = DEFAULT_GVISOR_OOM_THRESHOLD_PERCENT,
    already_triggered: bool = False,
) -> OomDecision:
    if memory_limit_bytes <= 0:
        return OomDecision(
            watcher=OomWatcherKind.ProcessMemory,
            triggered=False,
            reason="memory limit is not set",
        )
    usage_percent = memory_usage_bytes * 100.0 / memory_limit_bytes
    triggered = usage_percent >= threshold_percent and not already_triggered
    return OomDecision(
        watcher=OomWatcherKind.ProcessMemory,
        triggered=triggered,
        usage_percent=usage_percent,
        reason="memory usage exceeded threshold" if triggered else "memory usage below threshold",
    )


def _plan_runsc_command(
    config: RuntimeBinaryConfig,
    request: RuntimeCommandRequest,
) -> RuntimeCommandPlan:
    argv = _runsc_base_args(
        config,
        docker_enabled=request.docker_enabled,
        nvproxy=request.nvproxy,
    )
    cleanup_argv: list[str] | None = None
    operation = request.operation
    if operation is RuntimeOperation.Run:
        _require(request.container_id, "container_id")
        _require(request.bundle_path, "bundle_path")
        argv.extend(["run", "--bundle", str(request.bundle_path), str(request.container_id)])
        cleanup_argv = [*_runsc_base_args(config), "delete", "--force", str(request.container_id)]
    elif operation is RuntimeOperation.Exec:
        _require(request.container_id, "container_id")
        _require(request.process_spec_path, "process_spec_path")
        argv.extend(
            ["exec", "--process", str(request.process_spec_path), str(request.container_id)]
        )
    elif operation is RuntimeOperation.Kill:
        _require(request.container_id, "container_id")
        argv.append("kill")
        if request.all_processes:
            argv.append("--all")
        argv.extend([str(request.container_id), str(request.signal)])
    elif operation is RuntimeOperation.Delete:
        _require(request.container_id, "container_id")
        argv.append("delete")
        if request.force:
            argv.append("--force")
        argv.append(str(request.container_id))
    elif operation is RuntimeOperation.State:
        _require(request.container_id, "container_id")
        argv.extend(["state", str(request.container_id)])
    elif operation is RuntimeOperation.List:
        argv.extend(["list", "--format=json"])
    elif operation is RuntimeOperation.Checkpoint:
        _require(request.container_id, "container_id")
        argv.append("checkpoint")
        _append_path_arg(argv, "--image-path", request.image_path)
        _append_path_arg(argv, "--work-path", request.work_dir)
        if request.leave_running:
            argv.append("--leave-running")
        argv.append(str(request.container_id))
    elif operation is RuntimeOperation.Restore:
        _require(request.container_id, "container_id")
        argv.append("restore")
        _append_path_arg(argv, "--image-path", request.image_path)
        _append_path_arg(argv, "--work-path", request.work_dir)
        _append_path_arg(argv, "--bundle", request.bundle_path)
        argv.append(str(request.container_id))
        cleanup_argv = [*_runsc_base_args(config), "delete", "--force", str(request.container_id)]
    return RuntimeCommandPlan(
        runtime=OciRuntimeName.Runsc,
        operation=operation,
        argv=argv,
        uses_process_group=operation in {RuntimeOperation.Run, RuntimeOperation.Restore},
        cleanup_argv=cleanup_argv,
    )


def _runsc_base_args(
    config: RuntimeBinaryConfig,
    *,
    docker_enabled: bool = False,
    nvproxy: bool = False,
) -> list[str]:
    args = [config.runsc_path, "--root", config.runsc_root]
    if config.debug:
        args.extend(["--debug", "--debug-log", posixpath.join(config.runsc_root, "debug.log")])
    if config.runsc_platform:
        args.extend(["--platform", config.runsc_platform])
    args.extend(config.runsc_extra_args)
    if docker_enabled:
        args.append("--net-raw")
    # Without this the sandbox has no NVIDIA driver proxy, so the device nodes in
    # the bundle open onto nothing. Deliberately not paired with
    # --nvproxy-allow-unsupported-driver: nvproxy validates the driver ABI it was
    # built against, and a mismatch must fail by name rather than run unproven.
    if nvproxy:
        args.append("--nvproxy")
    return args


def _base_capabilities() -> JsonObject:
    base = _json_string_list(
        [
            "CAP_AUDIT_WRITE",
            "CAP_KILL",
            "CAP_NET_RAW",
            "CAP_CHOWN",
            "CAP_DAC_OVERRIDE",
            "CAP_FSETID",
            "CAP_FOWNER",
            "CAP_SETGID",
            "CAP_SETUID",
            "CAP_SETFCAP",
            "CAP_SYS_CHROOT",
            "CAP_MKNOD",
        ]
    )
    ambient: JsonArray = []
    return {
        "bounding": list(base),
        "effective": list(base),
        "permitted": list(base),
        "ambient": ambient,
    }


def _namespace(namespace: str) -> JsonObject:
    return {"type": namespace}


def _json_string_list(values: list[str]) -> JsonArray:
    result: JsonArray = list(values)
    return result


def _mount(
    destination: str,
    mount_type: str,
    source: str,
    options: list[str],
) -> JsonObject:
    return {
        "destination": destination,
        "type": mount_type,
        "source": source,
        "options": _json_string_list(options),
    }


def _ensure_dict(target: JsonObject, key: str) -> JsonObject:
    value = target.get(key)
    if isinstance(value, dict):
        return value
    replacement: JsonObject = {}
    target[key] = replacement
    return replacement


def _ensure_list(target: JsonObject, key: str) -> JsonArray:
    value = target.get(key)
    if isinstance(value, list):
        return value
    replacement: JsonArray = []
    target[key] = replacement
    return replacement


def _merge_unique(existing: list[str], additions: list[str]) -> list[str]:
    seen = set(existing)
    merged = list(existing)
    for item in additions:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


def _decode_json_object(payload: str | bytes | JsonObject) -> JsonObject:
    if isinstance(payload, dict):
        return _JSON_OBJECT_ADAPTER.validate_python(payload)
    return _JSON_OBJECT_ADAPTER.validate_json(payload)


def _parse_status(value: JsonValue | None) -> RuntimeContainerStatus:
    raw = str(value or "").strip().lower()
    try:
        return RuntimeContainerStatus(raw)
    except ValueError:
        return RuntimeContainerStatus.Unknown


def _append_path_arg(argv: list[str], flag: str, value: str | None) -> None:
    if value:
        argv.extend([flag, value])


def _parse_int(value: JsonValue | None, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool | int | float | str):
        try:
            return int(value or 0)
        except ValueError as exc:
            msg = f"container runtime {field_name} must be an integer"
            raise ValueError(msg) from exc
    msg = f"container runtime {field_name} must be a JSON scalar"
    raise ValueError(msg)


def _require(value: str | None, field_name: str) -> None:
    if value in {None, ""}:
        msg = f"{field_name} is required for container runtime command"
        raise ValueError(msg)


DOCKER_IN_DOCKER_CAPABILITIES = [
    "CAP_AUDIT_WRITE",
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_KILL",
    "CAP_MKNOD",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_ADMIN",
    "CAP_NET_RAW",
    "CAP_SETFCAP",
    "CAP_SETGID",
    "CAP_SETPCAP",
    "CAP_SETUID",
    "CAP_SYS_ADMIN",
    "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE",
]

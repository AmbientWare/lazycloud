from __future__ import annotations

import hashlib
import re
import sys
from datetime import datetime
from enum import StrEnum
from html import escape
from pathlib import Path

from pydantic import Field
from shared.app_identity import (
    AGENT_LAUNCHD_LABEL_PREFIX,
    AGENT_NAME,
    AGENT_SERVICE_DESCRIPTION,
    STATE_DIR,
)
from shared.compute_enrollment import PreflightSeverity
from shared.contracts import ContractModel
from shared.timestamps import utc_now

DEFAULT_AGENT_SERVICE_NAME = AGENT_NAME
DEFAULT_AGENT_SERVICE_DESCRIPTION = AGENT_SERVICE_DESCRIPTION
DEFAULT_AGENT_STATE_DIR = f"{STATE_DIR}/agent"
DEFAULT_AGENT_SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_SYSTEMD_UNIT_DIR = "/etc/systemd/system"
DEFAULT_LAUNCHD_SYSTEM_DIR = "/Library/LaunchDaemons"
DEFAULT_LAUNCHD_USER_DIR = "Library/LaunchAgents"
DEFAULT_LAUNCHD_LABEL_PREFIX = AGENT_LAUNCHD_LABEL_PREFIX
TELEMETRY_BATCH_SIZE = 128
TELEMETRY_BUFFER_SIZE = 1024
TRANSPORT_FULL_SNAPSHOT_EVERY = 5
TRANSPORT_FAILURE_THRESHOLD = 3
TRANSPORT_FAILURE_INTERVAL_SECONDS = 600
MAX_TRANSPORT_ATTR_LENGTH = 240
UNLIMITED_CGROUP_V1_SENTINEL = 1 << 62

_INVALID_SERVICE_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_.@-]+")


class ServicePlatform(StrEnum):
    Systemd = "systemd"
    Launchd = "launchd"
    Shell = "shell"


class ServiceLifecycleAction(StrEnum):
    Start = "start"
    Restart = "restart"
    Status = "status"
    Leave = "leave"
    Uninstall = "uninstall"


class PreflightStatus(StrEnum):
    Passed = "passed"
    Failed = "failed"
    Warning = "warning"


class PreflightCheckName(StrEnum):
    PythonVersion = "python-version"
    K3s = "k3s"
    Flux = "flux"
    TailnetDaemon = "tailnet-daemon"
    LocalDev = "local-dev"
    AgentContainer = "agent-container"
    Linux = "linux"
    Root = "root"
    ContainerRuntime = "container-runtime"
    DockerSocket = "docker-socket"
    DockerDaemon = "docker-daemon"
    DockerHostNetwork = "docker-host-network"
    NetworkNamespace = "network-namespace"
    NetnsRunDir = "netns-run-dir"
    IpForward = "ip-forward"
    Iptables = "iptables"
    NetworkManager = "network-manager"
    Fuse = "fuse"
    NvidiaRuntime = "nvidia-runtime"


class TransportKind(StrEnum):
    Local = "local"
    Http = "http"
    Tunnel = "tunnel"
    Tailnet = "tailnet"


class TransportSnapshotFailureKind(StrEnum):
    Canceled = "canceled"
    DeadlineExceeded = "deadline_exceeded"
    StatusUnavailable = "status_unavailable"


class PreflightCheck(ContractModel):
    name: str
    status: PreflightStatus
    message: str
    severity: PreflightSeverity = PreflightSeverity.Info

    @property
    def ok(self) -> bool:
        return self.status == PreflightStatus.Passed


class AgentPreflightProbeSet(ContractModel):
    os_name: str = sys.platform
    dev_mode: bool = False
    worker_container_executor: bool = True
    agent_in_container: bool = False
    effective_uid: int = 0
    docker_command: bool = False
    docker_socket: bool = False
    docker_daemon: bool = False
    docker_host_network: bool = False
    network_namespace: bool = False
    netns_run_dir_writable: bool = False
    ip_forward_enabled_or_writable: bool = False
    iptables_nat: bool = False
    network_manager: bool = False
    fuse_device: bool = False
    nvidia_gpu_names: list[str] = Field(default_factory=list)
    nvidia_runtime: bool = False


class AgentPreflightPlan(ContractModel):
    checks: list[PreflightCheck]
    gpus: list[str] = Field(default_factory=list)
    schedulable: bool = False


class AgentServiceSpec(ContractModel):
    name: str = DEFAULT_AGENT_SERVICE_NAME
    description: str = DEFAULT_AGENT_SERVICE_DESCRIPTION
    binary_path: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    state_dir: str = DEFAULT_AGENT_STATE_DIR


class RenderedServiceInstallPlan(ContractModel):
    platform: ServicePlatform
    spec: AgentServiceSpec
    target_path: str
    content: str
    commands: list[list[str]]


class ServiceCommand(ContractModel):
    argv: list[str]
    allow_failure: bool = False


class AgentServiceLifecyclePlan(ContractModel):
    action: ServiceLifecycleAction
    platform: ServicePlatform
    service_name: str
    target_path: str
    before_removal: list[ServiceCommand] = Field(default_factory=list)
    remove_service: bool = False
    remove_state: bool = False
    after_removal: list[ServiceCommand] = Field(default_factory=list)


class AgentServiceRuntimeStatus(ContractModel):
    platform: ServicePlatform
    service_name: str
    target_path: str
    installed: bool = False
    active: bool = False
    enabled: bool = False


class ServiceCommandResult(ContractModel):
    argv: list[str]
    returncode: int


class AgentServiceInstallResult(ContractModel):
    platform: ServicePlatform
    service_name: str
    service_path: str
    command: list[str]
    service_commands: list[list[str]] = Field(default_factory=list)
    commands: list[ServiceCommandResult] = Field(default_factory=list)
    installed: bool = False
    dry_run: bool = False


class AgentRemoteLeaveResult(ContractModel):
    machine_id: str
    completed: bool = True
    already_absent: bool = False


class AgentServiceOperationResult(ContractModel):
    action: ServiceLifecycleAction
    platform: ServicePlatform
    service_name: str
    service_path: str
    commands: list[ServiceCommandResult] = Field(default_factory=list)
    remote_leave: AgentRemoteLeaveResult | None = None
    service_removed: bool = False
    state_removed: bool = False
    binary_removed: bool = False


class TransportPeerStatus(ContractModel):
    online: bool = False
    active: bool = False
    direct: bool = False
    relay: str = ""
    last_handshake_age_ms: int | None = None


class TransportStatusSnapshot(ContractModel):
    backend_state: str = ""
    health: list[str] = Field(default_factory=list)
    self_dns: str = ""
    self_online: bool = False
    self_relay: str = ""
    tailnet_ips: list[str] = Field(default_factory=list)
    peers: list[TransportPeerStatus] = Field(default_factory=list)


class TransportFailureDecision(ContractModel):
    emit: bool
    kind: TransportSnapshotFailureKind | None = None
    message: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)


class AgentLogRecord(ContractModel):
    source: str = "agent"
    worker_id: str = ""
    level: str = "info"
    stream: str = "stdout"
    line: str
    timestamp_unix_nano: int = Field(default_factory=lambda: _unix_nano(utc_now()))


class AgentEventRecord(ContractModel):
    event_type: str
    action: str
    status: str = ""
    message: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)
    timestamp_unix_nano: int = Field(default_factory=lambda: _unix_nano(utc_now()))


class AgentMetricSnapshot(ContractModel):
    timestamp_unix_nano: int = Field(default_factory=lambda: _unix_nano(utc_now()))
    cpu_utilization_pct: float = 0.0
    memory_used_mb: int = 0
    memory_total_mb: int = 0
    memory_utilization_pct: float = 0.0
    disk_used_mb: int = 0
    disk_total_mb: int = 0
    disk_usage_pct: float = 0.0
    disk_path: str = "/"
    network_recv_bytes: int = 0
    network_sent_bytes: int = 0
    network_recv_packets: int = 0
    network_sent_packets: int = 0
    worker_count: int = 0
    container_count: int = 0
    free_gpu_count: int = 0


class AgentTelemetryRequest(ContractModel):
    agent_token: str = ""
    logs: list[AgentLogRecord] = Field(default_factory=list)
    events: list[AgentEventRecord] = Field(default_factory=list)
    metrics: AgentMetricSnapshot | None = None


class AgentTelemetryBatch(ContractModel):
    logs: list[AgentLogRecord] = Field(default_factory=list)
    events: list[AgentEventRecord] = Field(default_factory=list)
    metrics: AgentMetricSnapshot | None = None

    def add(self, request: AgentTelemetryRequest | None) -> None:
        if request is None:
            return
        self.logs.extend(request.logs)
        self.events.extend(request.events)
        if request.metrics is not None:
            self.metrics = request.metrics

    def size(self) -> int:
        return telemetry_record_count(self.logs, self.events, self.metrics)

    def request(self, agent_token: str) -> AgentTelemetryRequest:
        return AgentTelemetryRequest(
            agent_token=agent_token,
            logs=self.logs,
            events=self.events,
            metrics=self.metrics,
        )


class LineBufferPlan(ContractModel):
    lines: list[str] = Field(default_factory=list)
    remainder: str = ""


class ServiceUnit(ContractModel):
    name: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    working_directory: str | None = None
    restart: bool = True


class ServiceInstallPlan(ContractModel):
    platform: ServicePlatform
    unit: ServiceUnit
    commands: list[list[str]]


class LogWrite(ContractModel):
    stream: str = "system"
    message: str
    created_at: datetime = Field(default_factory=utc_now)


def detect_platform() -> ServicePlatform:
    if sys.platform == "darwin":
        return ServicePlatform.Launchd
    if sys.platform.startswith("linux"):
        return ServicePlatform.Systemd
    return ServicePlatform.Shell


def resolve_service_platform(
    requested: str,
    *,
    os_name: str | None = None,
) -> ServicePlatform:
    value = requested.strip().lower().replace("_", "-") or "auto"
    platform_name = (os_name or sys.platform).strip().lower()
    if value == "auto":
        if platform_name.startswith("linux"):
            return ServicePlatform.Systemd
        if platform_name == "darwin":
            return ServicePlatform.Launchd
        msg = f"no supported service manager for {platform_name or 'unknown operating system'}"
        raise ValueError(msg)
    if value == ServicePlatform.Systemd.value:
        return ServicePlatform.Systemd
    if value == ServicePlatform.Launchd.value:
        return ServicePlatform.Launchd
    msg = f"unsupported service manager: {requested}"
    raise ValueError(msg)


def build_service_install_plan(
    unit: ServiceUnit,
    *,
    platform: ServicePlatform | None = None,
) -> ServiceInstallPlan:
    selected = platform or detect_platform()
    if selected == ServicePlatform.Systemd:
        commands = [["systemctl", "enable", unit.name], ["systemctl", "restart", unit.name]]
    elif selected == ServicePlatform.Launchd:
        commands = [["launchctl", "bootstrap", "gui/$UID", f"{unit.name}.plist"]]
    else:
        commands = [unit.command]
    return ServiceInstallPlan(platform=selected, unit=unit, commands=commands)


def normalize_service_name(name: str) -> str:
    value = name.strip() or DEFAULT_AGENT_SERVICE_NAME
    value = _INVALID_SERVICE_NAME_CHARS.sub("-", value)
    value = value.strip("-_.@")
    return value or DEFAULT_AGENT_SERVICE_NAME


def normalize_agent_service_spec(spec: AgentServiceSpec) -> AgentServiceSpec:
    env = {"PATH": DEFAULT_AGENT_SERVICE_PATH}
    env.update({key.strip(): value for key, value in spec.env.items() if key.strip()})
    env.setdefault("AGENT_HOME", spec.state_dir)
    env.setdefault("XDG_CONFIG_HOME", f"{spec.state_dir.rstrip('/')}/.config")
    return spec.model_copy(
        update={
            "name": normalize_service_name(spec.name),
            "description": spec.description.strip() or DEFAULT_AGENT_SERVICE_DESCRIPTION,
            "binary_path": spec.binary_path.strip(),
            "state_dir": str(Path(spec.state_dir or DEFAULT_AGENT_STATE_DIR)),
            "env": env,
        }
    )


def render_systemd_unit(spec: AgentServiceSpec) -> str:
    spec = normalize_agent_service_spec(spec)
    lines = [
        "[Unit]",
        f"Description={spec.description}",
        "Wants=network-online.target docker.service",
        "After=network-online.target docker.service",
        # A machine that cannot reach the control plane must keep trying, so the
        # limit is sized to outlast a full bootstrap phase deadline of gateway
        # unavailability (300s at RestartSec=15 is 20 starts; 40 doubles it).
        # Unbounded is the wrong answer to that: a revoked agent is rejected by
        # every join it will ever attempt, and with no limit it retried forever
        # on a machine that keeps billing.
        "StartLimitIntervalSec=600",
        "StartLimitBurst=40",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={systemd_path(spec.state_dir)}",
    ]
    for key in sorted(spec.env):
        lines.append(f"Environment={systemd_quote(f'{key}={spec.env[key]}')}")
    lines.extend(
        [
            f"ExecStart={systemd_command([spec.binary_path, *spec.args])}",
            # `on-failure`, not `always`: the agent exits non-zero for every
            # reason worth retrying, and a revoked one must be allowed to stop.
            "Restart=on-failure",
            "RestartSec=15",
            "KillSignal=SIGINT",
            "TimeoutStopSec=30",
            "LimitNOFILE=1048576",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    return "\n".join(lines)


def render_launchd_plist(spec: AgentServiceSpec, *, root: bool = False) -> str:
    spec = normalize_agent_service_spec(spec)
    label = launchd_label(spec.name)
    env_lines: list[str] = []
    for key in sorted(spec.env):
        env_lines.extend(
            [
                f"    <key>{escape(key)}</key>",
                f"    <string>{escape(spec.env[key])}</string>",
            ]
        )
    args = "\n".join(
        f"    <string>{escape(value)}</string>" for value in [spec.binary_path, *spec.args]
    )
    env_xml = "\n".join(env_lines)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{escape(label)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args}\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_xml}\n"
        "  </dict>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{escape(spec.state_dir)}</string>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{escape(spec.state_dir + '/agent.log')}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{escape(spec.state_dir + '/agent.err.log')}</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <dict>\n"
        "    <key>SuccessfulExit</key>\n"
        "    <false/>\n"
        "  </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def build_agent_service_install_plan(
    spec: AgentServiceSpec,
    *,
    platform: ServicePlatform,
    root: bool = False,
    uid: int = 501,
) -> RenderedServiceInstallPlan:
    spec = normalize_agent_service_spec(spec)
    if platform is ServicePlatform.Systemd:
        unit_name = f"{spec.name}.service"
        target_path = f"{DEFAULT_SYSTEMD_UNIT_DIR}/{unit_name}"
        commands = [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", unit_name],
            ["systemctl", "restart", unit_name],
        ]
        content = render_systemd_unit(spec)
    elif platform is ServicePlatform.Launchd:
        label = launchd_label(spec.name)
        target_path = (
            f"{DEFAULT_LAUNCHD_SYSTEM_DIR}/{label}.plist"
            if root
            else f"~/{DEFAULT_LAUNCHD_USER_DIR}/{label}.plist"
        )
        domain = "system" if root else f"gui/{uid}"
        commands = [
            ["launchctl", "bootout", domain, target_path],
            ["launchctl", "bootstrap", domain, target_path],
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        ]
        content = render_launchd_plist(spec, root=root)
    else:
        target_path = ""
        commands = [[spec.binary_path, *spec.args]]
        content = ""
    return RenderedServiceInstallPlan(
        platform=platform,
        spec=spec,
        target_path=target_path,
        content=content,
        commands=commands,
    )


def build_agent_service_lifecycle_plan(
    action: ServiceLifecycleAction,
    *,
    platform: ServicePlatform,
    service_name: str = DEFAULT_AGENT_SERVICE_NAME,
    root: bool = False,
    uid: int = 501,
) -> AgentServiceLifecyclePlan:
    name = normalize_service_name(service_name)
    if platform is ServicePlatform.Systemd:
        unit_name = f"{name}.service"
        target_path = f"{DEFAULT_SYSTEMD_UNIT_DIR}/{unit_name}"
        if action is ServiceLifecycleAction.Start:
            before = [ServiceCommand(argv=["systemctl", "start", unit_name])]
        elif action is ServiceLifecycleAction.Restart:
            before = [ServiceCommand(argv=["systemctl", "restart", unit_name])]
        elif action is ServiceLifecycleAction.Status:
            before = [
                ServiceCommand(argv=["systemctl", "is-active", unit_name], allow_failure=True),
                ServiceCommand(argv=["systemctl", "is-enabled", unit_name], allow_failure=True),
            ]
        else:
            before = [
                ServiceCommand(
                    argv=["systemctl", "disable", "--now", unit_name],
                    allow_failure=True,
                )
            ]
        destructive = action in {
            ServiceLifecycleAction.Leave,
            ServiceLifecycleAction.Uninstall,
        }
        after = (
            [
                ServiceCommand(argv=["systemctl", "daemon-reload"]),
                ServiceCommand(
                    argv=["systemctl", "reset-failed", unit_name],
                    allow_failure=True,
                ),
            ]
            if destructive
            else []
        )
    elif platform is ServicePlatform.Launchd:
        label = launchd_label(name)
        target_path = (
            f"{DEFAULT_LAUNCHD_SYSTEM_DIR}/{label}.plist"
            if root
            else f"~/{DEFAULT_LAUNCHD_USER_DIR}/{label}.plist"
        )
        domain = "system" if root else f"gui/{uid}"
        service_target = f"{domain}/{label}"
        if action is ServiceLifecycleAction.Start:
            before = [ServiceCommand(argv=["launchctl", "kickstart", service_target])]
        elif action is ServiceLifecycleAction.Restart:
            before = [ServiceCommand(argv=["launchctl", "kickstart", "-k", service_target])]
        elif action is ServiceLifecycleAction.Status:
            before = [
                ServiceCommand(argv=["launchctl", "print", service_target], allow_failure=True)
            ]
        else:
            before = [
                ServiceCommand(
                    argv=["launchctl", "bootout", domain, target_path],
                    allow_failure=True,
                )
            ]
        destructive = action in {
            ServiceLifecycleAction.Leave,
            ServiceLifecycleAction.Uninstall,
        }
        after = []
    else:
        msg = "foreground agents do not have a managed service lifecycle"
        raise ValueError(msg)
    return AgentServiceLifecyclePlan(
        action=action,
        platform=platform,
        service_name=name,
        target_path=target_path,
        before_removal=before,
        remove_service=destructive,
        remove_state=destructive,
        after_removal=after,
    )


def systemd_quote(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("%", "%%")
        + '"'
    )


def systemd_path(value: str) -> str:
    return value.replace("%", "%%")


def systemd_command(args: list[str]) -> str:
    return " ".join(systemd_quote(arg) for arg in args)


def launchd_label(service_name: str) -> str:
    name = normalize_service_name(service_name)
    if name == DEFAULT_AGENT_SERVICE_NAME:
        return DEFAULT_LAUNCHD_LABEL_PREFIX
    return f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{name}"


def run_preflight_checks(paths: list[Path] | None = None) -> list[PreflightCheck]:
    checks = [
        PreflightCheck(
            name=PreflightCheckName.PythonVersion,
            status=PreflightStatus.Passed
            if sys.version_info >= (3, 12)
            else PreflightStatus.Failed,
            message=f"python {sys.version_info.major}.{sys.version_info.minor}",
            severity=PreflightSeverity.Info
            if sys.version_info >= (3, 12)
            else PreflightSeverity.Error,
        )
    ]
    for path in paths or []:
        checks.append(
            PreflightCheck(
                name=f"path:{path}",
                status=PreflightStatus.Passed if path.exists() else PreflightStatus.Warning,
                message="exists" if path.exists() else "missing",
            )
        )
    return checks


def plan_agent_preflight(probes: AgentPreflightProbeSet) -> AgentPreflightPlan:
    checks = [
        _preflight_check(
            PreflightCheckName.K3s,
            True,
            "not required for agent worker-container mode",
            required=False,
        ),
        _preflight_check(
            PreflightCheckName.Flux,
            True,
            "not installed or required for agent worker-container mode",
            required=False,
        ),
        _preflight_check(
            PreflightCheckName.TailnetDaemon,
            True,
            "tailnet transport requires a tailscale daemon, sidecar, or managed tailscaled",
            required=False,
        ),
    ]
    if probes.dev_mode:
        checks.append(
            _preflight_check(
                PreflightCheckName.LocalDev,
                True,
                "local joins use the embedded route listener",
                required=False,
            )
        )
    if probes.agent_in_container:
        checks.append(
            _preflight_check(
                PreflightCheckName.AgentContainer,
                True,
                "containerized agent starts worker containers through the Docker socket",
                required=False,
            )
        )

    production_linux = probes.os_name == "linux"
    linux_ok = production_linux or (probes.dev_mode and not probes.worker_container_executor)
    checks.append(
        _preflight_check(
            PreflightCheckName.Linux,
            linux_ok,
            "worker-container execution requires Linux",
        )
    )

    if production_linux and probes.worker_container_executor:
        checks.extend(
            [
                _preflight_check(
                    PreflightCheckName.Root,
                    probes.effective_uid == 0,
                    "root is optional when rootful Docker is available",
                    required=False,
                ),
                _preflight_check(
                    PreflightCheckName.ContainerRuntime,
                    probes.docker_command,
                    "requires Docker for the worker-container executor",
                ),
                _preflight_check(
                    PreflightCheckName.DockerDaemon,
                    probes.docker_daemon,
                    "requires access to a running Docker daemon",
                ),
                _preflight_check(
                    PreflightCheckName.DockerHostNetwork,
                    probes.docker_host_network,
                    "requires Docker host networking for routed worker ports",
                ),
                _preflight_check(
                    PreflightCheckName.NetworkNamespace,
                    probes.network_namespace,
                    "requires container network namespaces",
                ),
                _preflight_check(
                    PreflightCheckName.NetnsRunDir,
                    probes.netns_run_dir_writable,
                    "requires writable /var/run/netns",
                ),
                _preflight_check(
                    PreflightCheckName.IpForward,
                    probes.ip_forward_enabled_or_writable,
                    "requires IPv4 forwarding for bridge NAT and exposed ports",
                ),
                _preflight_check(
                    PreflightCheckName.Iptables,
                    probes.iptables_nat,
                    "requires usable IPv4 nat/filter tables",
                ),
                _preflight_check(
                    PreflightCheckName.NetworkManager,
                    True,
                    network_manager_message(probes.network_manager),
                    required=False,
                ),
                _preflight_check(
                    PreflightCheckName.Fuse,
                    probes.fuse_device or probes.dev_mode,
                    "requires FUSE for lazy image mounts",
                ),
            ]
        )
        if probes.agent_in_container:
            checks.append(
                _preflight_check(
                    PreflightCheckName.DockerSocket,
                    probes.docker_socket,
                    "requires /var/run/docker.sock for sibling worker containers",
                )
            )

    if probes.nvidia_gpu_names:
        checks.append(
            _preflight_check(
                PreflightCheckName.NvidiaRuntime,
                probes.nvidia_runtime,
                "required to pin GPUs into worker containers",
            )
        )
    return AgentPreflightPlan(
        checks=checks,
        gpus=probes.nvidia_gpu_names,
        schedulable=all_required_preflight_checks_passed(checks),
    )


def all_required_preflight_checks_passed(checks: list[PreflightCheck]) -> bool:
    return all(check.severity is not PreflightSeverity.Error or check.ok for check in checks)


def network_manager_message(present: bool) -> str:
    if not present:
        return "not detected; worker-managed bridge and veth devices will be created directly"
    return "detected; worker-managed bridge and veth devices do not require NetworkManager profiles"


def machine_fingerprint(
    hostname: str,
    *,
    os_name: str,
    arch: str,
    env_fingerprint: str = "",
    machine_ids: list[str] | None = None,
) -> str:
    if env_fingerprint.strip():
        return env_fingerprint.strip()
    for value in machine_ids or []:
        if value.strip():
            return value.strip()
    payload = f"{os_name}\x00{arch}\x00{hostname}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def system_memory_mb_from_linux_meminfo(text: str) -> int:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "MemTotal:":
            return int(fields[1]) // 1024
    return 0


def system_memory_mb_from_darwin_sysctl(text: str) -> int:
    stripped = text.strip()
    return int(stripped) // 1024 // 1024 if stripped.isdigit() else 0


def normalize_transport(value: str | TransportKind) -> TransportKind:
    raw = value.value if isinstance(value, TransportKind) else value
    normalized = raw.strip().lower().replace("_", "-")
    if normalized in {"", "tailnet", "tsnet", "tailscale"}:
        return TransportKind.Tailnet
    if normalized == "http":
        return TransportKind.Http
    if normalized == "local":
        return TransportKind.Local
    if normalized == "tunnel":
        return TransportKind.Tunnel
    msg = f"unsupported agent transport: {raw}"
    raise ValueError(msg)


def transport_snapshot_full(tick: int) -> bool:
    return tick == 1 or (tick > 0 and tick % TRANSPORT_FULL_SNAPSHOT_EVERY == 0)


def transport_snapshot_attrs(
    snapshot: TransportStatusSnapshot,
    *,
    proxy_target: str,
    full: bool,
) -> dict[str, str]:
    attrs = {
        "proxy_target": proxy_target,
        "backend_state": snapshot.backend_state,
        "full_snapshot": str(full).lower(),
        "health_count": str(len(snapshot.health)),
    }
    health = join_attr_values(snapshot.health)
    if health:
        attrs["health"] = health
    if full:
        attrs["peer_count"] = str(len(snapshot.peers))
    if snapshot.self_dns:
        attrs["self_dns"] = snapshot.self_dns.rstrip(".")
        attrs["self_online"] = str(snapshot.self_online).lower()
        if snapshot.self_relay:
            attrs["self_relay"] = snapshot.self_relay
    if snapshot.tailnet_ips:
        attrs["tailnet_ips"] = ",".join(snapshot.tailnet_ips)
    if full:
        attrs.update(transport_peer_stats_attrs(snapshot.peers))
    return attrs


def transport_peer_stats_attrs(peers: list[TransportPeerStatus]) -> dict[str, str]:
    online = sum(1 for peer in peers if peer.online)
    active = sum(1 for peer in peers if peer.active)
    direct = sum(1 for peer in peers if peer.direct)
    relayed = sum(1 for peer in peers if not peer.direct and peer.relay)
    recent = sum(
        1
        for peer in peers
        if peer.last_handshake_age_ms is not None and peer.last_handshake_age_ms <= 120_000
    )
    ages = [
        peer.last_handshake_age_ms
        for peer in peers
        if peer.last_handshake_age_ms is not None and peer.last_handshake_age_ms >= 0
    ]
    attrs = {
        "online_peer_count": str(online),
        "direct_peer_count": str(direct),
        "relay_peer_count": str(relayed),
        "active_peer_count": str(active),
        "recent_handshake_peer_count": str(recent),
    }
    if ages:
        attrs["newest_handshake_age_ms"] = str(min(ages))
    relay_regions = sorted({peer.relay for peer in peers if peer.relay})
    if relay_regions:
        attrs["relay_regions"] = ",".join(relay_regions)
    return attrs


def join_attr_values(values: list[str], *, max_len: int = MAX_TRANSPORT_ATTR_LENGTH) -> str:
    joined = "; ".join(values)
    if len(joined) > max_len:
        return joined[: max_len - 3] + "..."
    return joined


def transport_snapshot_failure(
    error_message: str,
    *,
    canceled: bool = False,
    deadline: bool = False,
) -> tuple[TransportSnapshotFailureKind | None, str]:
    if not error_message and not canceled and not deadline:
        return (None, "")
    if canceled:
        return (TransportSnapshotFailureKind.Canceled, "")
    if deadline or "deadline exceeded" in error_message.lower():
        return (TransportSnapshotFailureKind.DeadlineExceeded, "transport snapshot timed out")
    return (TransportSnapshotFailureKind.StatusUnavailable, "transport snapshot unavailable")


def should_emit_transport_failure(
    *,
    proxy_target: str,
    failure_count: int,
    last_failure_event_age_seconds: int | None,
    error_message: str,
    deadline: bool = False,
    canceled: bool = False,
) -> TransportFailureDecision:
    kind, message = transport_snapshot_failure(
        error_message,
        deadline=deadline,
        canceled=canceled,
    )
    if not message or kind is None:
        return TransportFailureDecision(emit=False, kind=kind, message=message)
    if failure_count < TRANSPORT_FAILURE_THRESHOLD:
        return TransportFailureDecision(emit=False, kind=kind, message=message)
    if (
        last_failure_event_age_seconds is not None
        and last_failure_event_age_seconds < TRANSPORT_FAILURE_INTERVAL_SECONDS
    ):
        return TransportFailureDecision(emit=False, kind=kind, message=message)
    return TransportFailureDecision(
        emit=True,
        kind=kind,
        message=message,
        attrs={
            "proxy_target": proxy_target,
            "error_kind": kind.value,
            "failure_count": str(failure_count),
            "snapshot_error": "true",
        },
    )


def telemetry_record_count(
    logs: list[AgentLogRecord],
    events: list[AgentEventRecord],
    metrics: AgentMetricSnapshot | None,
) -> int:
    return len(logs) + len(events) + (1 if metrics is not None else 0)


def agent_telemetry_request_size(request: AgentTelemetryRequest | None) -> int:
    if request is None:
        return 0
    size = telemetry_record_count(request.logs, request.events, request.metrics)
    return size or 1


def split_line_buffer(
    chunks: list[str],
    *,
    prefix: str = "",
    suffix: str = "",
    flush_final: bool = False,
    trim_carriage_return: bool = True,
) -> LineBufferPlan:
    buffer = ""
    lines: list[str] = []
    for chunk in chunks:
        buffer += chunk
        while "\n" in buffer or "\r" in buffer:
            newline_positions = [pos for pos in (buffer.find("\n"), buffer.find("\r")) if pos >= 0]
            pos = min(newline_positions)
            line = buffer[:pos]
            if trim_carriage_return:
                line = line.rstrip("\r")
            if line or prefix or suffix:
                lines.append(f"{prefix}{line}{suffix}")
            buffer = buffer[pos + 1 :]
    if flush_final and buffer:
        line = buffer.rstrip("\r") if trim_carriage_return else buffer
        lines.append(f"{prefix}{line}{suffix}")
        buffer = ""
    return LineBufferPlan(lines=lines, remainder=buffer)


def bytes_to_mib(value: int) -> int:
    return value // 1024 // 1024


def parse_cgroup_uint_text(text: str) -> int | None:
    stripped = text.strip()
    if not stripped or stripped == "max":
        return None
    try:
        value = int(stripped)
    except ValueError:
        return None
    if value >= UNLIMITED_CGROUP_V1_SENTINEL:
        return None
    return value


def cgroup_memory_override(
    *,
    host_used_bytes: int,
    host_total_bytes: int,
    cgroup_used_bytes: int | None,
    cgroup_limit_bytes: int | None,
) -> tuple[int, int]:
    if (
        cgroup_used_bytes is not None
        and cgroup_limit_bytes is not None
        and 0 < cgroup_limit_bytes < host_total_bytes
    ):
        return (cgroup_used_bytes, cgroup_limit_bytes)
    return (host_used_bytes, host_total_bytes)


def _preflight_check(
    name: PreflightCheckName,
    ok: bool,
    message: str,
    *,
    required: bool = True,
) -> PreflightCheck:
    if ok:
        status = PreflightStatus.Passed
        severity = PreflightSeverity.Info
    elif required:
        status = PreflightStatus.Failed
        severity = PreflightSeverity.Error
    else:
        status = PreflightStatus.Warning
        severity = PreflightSeverity.Info
    return PreflightCheck(
        name=name.value,
        status=status,
        severity=severity,
        message=message,
    )


def _unix_nano(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def append_log(path: Path, entry: LogWrite) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{entry.created_at.isoformat()} [{entry.stream}] {entry.message}\n")
    return path

from __future__ import annotations

import ipaddress
import shutil
import socket
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from foundation.process import ProcessResult, run_process
from pydantic import Field
from shared.container_requests import WorkerStartupKind
from shared.contracts import ContractModel
from shared.scheduling import (
    ContainerIpAssignment,
    NetworkIpMutationPlan,
    WorkerRepositoryLockRecord,
    WorkerRepositoryLockRelease,
)

from worker.container_execution import (
    ContainerExecutionContext,
    ContainerNetworkSetupResult,
)
from worker.events import ContainerRequestContext
from worker.execution import (
    DEFAULT_CONTAINER_BRIDGE_NAME,
    DEFAULT_CONTAINER_IPV6_SUBNET,
    DEFAULT_CONTAINER_SUBNET,
    ContainerNetworkIdentity,
    NetworkAddressMode,
    PortBinding,
    container_ipv6_address,
    container_veth_names,
)
from worker.lifecycle import (
    HOST_RESOLV_CONF_PATH,
    WORKER_RESOLV_CONF_PATH,
    WORKER_SANDBOX_PROCESS_MANAGER_PORT,
    required_container_resolv_conf_source,
)
from worker.network_egress import WorkerNetworkEgressCounters
from worker.network_rules import (
    container_id_from_iptables_rule,
    container_network_comment,
    iptables_rule_fields,
    iptables_rule_matches_source_ip,
    iptables_rule_target,
)
from worker.network_slots import (
    ContainerNetworkInfo,
    NetworkRestrictionMode,
    NetworkRestrictionPlan,
    container_network_info_from_ip,
    plan_network_restriction,
)

DEFAULT_HOST_NETNS_PATH = "/var/run/netns"
DEFAULT_NETNS_CONFIG_ROOT = "/etc/netns"
DEFAULT_GATEWAY_EGRESS_TIMEOUT_SECONDS = 10
DEFAULT_NETWORK_LOCK_TTL_SECONDS = 30
DEFAULT_NETWORK_LOCK_RETRIES = 3


class AgentBridgeNetworkOperation(StrEnum):
    InspectBridge = "inspect-bridge"
    InspectDefaultRoute = "inspect-default-route"
    InspectGatewayRoute = "inspect-gateway-route"
    InspectFirewall = "inspect-firewall"
    EnableForwarding = "enable-forwarding"
    CheckFirewallRule = "check-firewall-rule"
    EnsureBridge = "ensure-bridge"
    CreateVethPair = "create-veth-pair"
    AttachHostVeth = "attach-host-veth"
    CreateNamespace = "create-namespace"
    MoveContainerVeth = "move-container-veth"
    ConfigureNamespace = "configure-namespace"
    EnableMasquerade = "enable-masquerade"
    AllowForwarding = "allow-forwarding"
    BlockProviderMetadata = "block-provider-metadata"
    ProbeGatewayEgress = "probe-gateway-egress"
    ExposePort = "expose-port"
    UnexposePort = "unexpose-port"
    ListExposedPorts = "list-exposed-ports"
    DeleteExposedPort = "delete-exposed-port"
    ListNetworkRestrictions = "list-network-restrictions"
    DeleteNetworkRestriction = "delete-network-restriction"
    DeleteOwnedForwardRule = "delete-owned-forward-rule"
    IsolateSandboxControl = "isolate-sandbox-control"
    ListSandboxControlIsolation = "list-sandbox-control-isolation"
    DeleteSandboxControlIsolation = "delete-sandbox-control-isolation"
    BlockNetwork = "block-network"
    AllowNetworkDestination = "allow-network-destination"
    DeleteNamespace = "delete-namespace"
    DeleteVeth = "delete-veth"


class NetworkCommand(ContractModel):
    operation: AgentBridgeNetworkOperation
    argv: list[str]
    ignore_failure: bool = False


class NetworkPolicyUpdateResult(ContractModel):
    container_id: str
    mode: NetworkRestrictionMode
    container_ip: str
    container_ipv6: str = ""
    operations: list[AgentBridgeNetworkOperation] = Field(default_factory=list)


class AgentBridgeNetworkConfig(ContractModel):
    bridge_name: str = DEFAULT_CONTAINER_BRIDGE_NAME
    subnet: str = DEFAULT_CONTAINER_SUBNET
    ipv6_subnet: str = DEFAULT_CONTAINER_IPV6_SUBNET
    host_netns_path: str = DEFAULT_HOST_NETNS_PATH
    netns_config_root: str = DEFAULT_NETNS_CONFIG_ROOT
    host_resolv_conf_path: str = HOST_RESOLV_CONF_PATH
    fallback_resolv_conf_path: str = WORKER_RESOLV_CONF_PATH
    ip_binary: str = "ip"
    iptables_binary: str = "iptables"
    ip6tables_binary: str = "ip6tables"
    sysctl_binary: str = "sysctl"
    python_binary: str = "python"
    enable_ipv6: bool = True
    gateway_egress_timeout_seconds: int = Field(
        default=DEFAULT_GATEWAY_EGRESS_TIMEOUT_SECONDS,
        ge=1,
        le=60,
    )

    @property
    def gateway(self) -> str:
        # Derived rather than configured: a gateway carried separately can be set
        # outside the subnet it is supposed to front, and a second bridge makes that
        # two values to keep in step instead of one.
        return str(next(ipaddress.ip_network(self.subnet, strict=False).hosts()))

    @property
    def gateway_ipv6(self) -> str:
        return str(next(ipaddress.ip_network(self.ipv6_subnet, strict=False).hosts()))

    @property
    def gateway_cidr(self) -> str:
        network = ipaddress.ip_network(self.subnet, strict=False)
        return f"{self.gateway}/{network.prefixlen}"

    @property
    def gateway_ipv6_cidr(self) -> str:
        network = ipaddress.ip_network(self.ipv6_subnet, strict=False)
        return f"{self.gateway_ipv6}/{network.prefixlen}"

    def container_cidr(self, ip_address: str) -> str:
        network = ipaddress.ip_network(self.subnet, strict=False)
        return f"{ip_address}/{network.prefixlen}"


class HostNetworkCapabilities(ContractModel):
    ipv4_interface: str
    ipv6_interface: str = ""
    # The interface that routes to the control plane's runtime endpoint when it
    # is not the default-route one. On a managed node the control plane is
    # reached over WireGuard, so container traffic to it leaves through the
    # tunnel interface and needs forwarding and NAT there, not on the uplink.
    gateway_interface: str = ""

    @property
    def ipv6_enabled(self) -> bool:
        return bool(self.ipv6_interface)


class ProbeNetworkReservation(ContractModel):
    ip_address: str
    reservation_id: str


class NetworkCommandRunner(Protocol):
    def __call__(self, args: list[str]) -> ProcessResult: ...


class NetworkIpAllocator(Protocol):
    worker_id: str
    """Names the veth pair a probe creates, so two workers cannot tear down
    each other's in-flight check."""

    def acquire_network_lock(self) -> str: ...

    def release_network_lock(self, token: str) -> None: ...

    def reserve_container_ip(self, container_id: str) -> str: ...

    def release_container_ip(self, container_id: str) -> None: ...

    def reserve_probe_ip(self) -> ProbeNetworkReservation: ...

    def release_probe_ip(self, reservation: ProbeNetworkReservation) -> None: ...


class SchedulerNetworkIpRepository(Protocol):
    def set_network_lock(
        self,
        network_prefix: str,
        *,
        ttl_seconds: int,
        retries: int,
    ) -> WorkerRepositoryLockRecord: ...

    def remove_network_lock(
        self,
        network_prefix: str,
        token: str,
    ) -> WorkerRepositoryLockRelease: ...

    def list_assignments(self, network_prefix: str) -> list[ContainerIpAssignment]: ...

    def set_container_ip(
        self, network_prefix: str, container_id: str, ip_address: str
    ) -> NetworkIpMutationPlan: ...

    def remove_container_ip(
        self,
        network_prefix: str,
        container_id: str,
    ) -> NetworkIpMutationPlan: ...


@dataclass(slots=True)
class SchedulerNetworkIpAllocator:
    repository: SchedulerNetworkIpRepository
    network_prefix: str
    subnet: str = DEFAULT_CONTAINER_SUBNET
    lock_ttl_seconds: int = DEFAULT_NETWORK_LOCK_TTL_SECONDS
    lock_retries: int = DEFAULT_NETWORK_LOCK_RETRIES
    worker_id: str = ""
    _next_offset: int = 0

    @property
    def gateway(self) -> str:
        """The address the bridge itself holds, and so the one no container may take."""

        return str(next(ipaddress.ip_network(self.subnet, strict=False).hosts()))

    def acquire_network_lock(self) -> str:
        lock = self.repository.set_network_lock(
            self.network_prefix,
            ttl_seconds=self.lock_ttl_seconds,
            retries=self.lock_retries,
        )
        if not lock.acquired or not lock.token:
            raise RuntimeError(f"network {self.network_prefix!r} lock was not acquired")
        return lock.token

    def release_network_lock(self, token: str) -> None:
        release = self.repository.remove_network_lock(self.network_prefix, token)
        if not release.released:
            detail = f": {release.reason}" if release.reason else ""
            raise RuntimeError(f"network {self.network_prefix!r} lock was not released{detail}")

    def _next_available_ip(self) -> str:
        assigned = self._assigned_ips()
        network = ipaddress.ip_network(self.subnet, strict=False)
        addresses = list(network.hosts())
        if not addresses:
            msg = f"network {self.subnet} has no usable container addresses"
            raise RuntimeError(msg)
        for _ in range(len(addresses)):
            index = self._next_offset % len(addresses)
            candidate = str(addresses[index])
            self._next_offset += 1
            if candidate == self.gateway or candidate in assigned:
                continue
            return candidate
        msg = f"network {self.subnet} has no available container addresses"
        raise RuntimeError(msg)

    def reserve_container_ip(self, container_id: str) -> str:
        token = self.acquire_network_lock()
        try:
            candidate = self._next_available_ip()
            self.repository.set_container_ip(
                self.network_prefix,
                container_id,
                candidate,
            )
            return candidate
        finally:
            self.release_network_lock(token)

    def release_container_ip(self, container_id: str) -> None:
        token = self.acquire_network_lock()
        try:
            self.repository.remove_container_ip(self.network_prefix, container_id)
        finally:
            self.release_network_lock(token)

    def reserve_probe_ip(self) -> ProbeNetworkReservation:
        """Record the probe's address the way a container's is recorded.

        Returning while still holding the lock is what kept another slot from
        taking the same address, but it also left the lock held for the whole
        probe — a network round trip — and the lock is node-wide with only a few
        short retries. A second slot validating readiness at the same time
        exhausted them, failed readiness, and never reported itself available.
        Writing the assignment reserves the address without the lock outliving
        the allocation it protects.

        The reservation is keyed by the worker's own id, not an invented one.
        A network mutation is authorized against the container it names, and a
        worker probing its own readiness names no container -- an invented id
        belonged to nothing, was refused, and left the worker unable to report
        ready at all.
        """
        if not self.worker_id:
            # Falling back to an invented id would reserve an address the server
            # refuses, and readiness would fail with the network named instead
            # of the missing identity.
            raise RuntimeError("worker id is required to reserve a readiness probe address")
        return ProbeNetworkReservation(
            ip_address=self.reserve_container_ip(self.worker_id),
            reservation_id=self.worker_id,
        )

    def release_probe_ip(self, reservation: ProbeNetworkReservation) -> None:
        self.release_container_ip(reservation.reservation_id)

    def _assigned_ips(self) -> set[str]:
        result: set[str] = set()
        for assignment in self.repository.list_assignments(self.network_prefix):
            value = getattr(assignment, "ip_address", "")
            if value:
                result.add(str(value))
        return result


@dataclass(slots=True)
class CommandNetworkSystem:
    run_command: NetworkCommandRunner = run_process
    commands: list[NetworkCommand] = field(default_factory=list)

    def run(self, command: NetworkCommand) -> ProcessResult:
        self.commands.append(command)
        result = self.run_command(command.argv)
        if not result.ok and not command.ignore_failure:
            msg = result.stderr or result.stdout or f"network command failed: {command.argv}"
            raise RuntimeError(msg)
        return result

    def discover_host_capabilities(
        self,
        config: AgentBridgeNetworkConfig,
        *,
        gateway_address: str = "",
    ) -> HostNetworkCapabilities:
        ipv4_route = self.run(
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.InspectDefaultRoute,
                argv=[config.ip_binary, "-4", "route", "show", "default"],
            )
        )
        ipv4_interface = default_route_interface(ipv4_route.stdout)
        if not ipv4_interface:
            raise RuntimeError("worker host has no IPv4 default-route interface")

        gateway_interface = ""
        if gateway_address:
            gateway_route = self.run(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.InspectGatewayRoute,
                    argv=[config.ip_binary, "-4", "route", "get", gateway_address],
                )
            )
            candidate = route_lookup_interface(gateway_route.stdout)
            if not candidate:
                raise RuntimeError(
                    f"worker host has no route to the control plane at {gateway_address}"
                )
            if candidate != ipv4_interface:
                gateway_interface = candidate

        for binary in (config.iptables_binary,):
            for table in ("nat", "filter"):
                self.run(
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.InspectFirewall,
                        argv=[binary, "-t", table, "-S"],
                    )
                )

        ipv6_interface = ""
        if config.enable_ipv6:
            ipv6_route = self.run(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.InspectDefaultRoute,
                    argv=[config.ip_binary, "-6", "route", "show", "default"],
                    ignore_failure=True,
                )
            )
            candidate = default_route_interface(ipv6_route.stdout) if ipv6_route.ok else ""
            if candidate:
                ipv6_firewall_ready = True
                for table in ("nat", "filter"):
                    result = self.run(
                        NetworkCommand(
                            operation=AgentBridgeNetworkOperation.InspectFirewall,
                            argv=[config.ip6tables_binary, "-t", table, "-S"],
                            ignore_failure=True,
                        )
                    )
                    ipv6_firewall_ready = ipv6_firewall_ready and result.ok
                if ipv6_firewall_ready:
                    ipv6_interface = candidate
        return HostNetworkCapabilities(
            ipv4_interface=ipv4_interface,
            ipv6_interface=ipv6_interface,
            gateway_interface=gateway_interface,
        )


def _gateway_ipv4_address(gateway_public_http_url: str) -> str:
    """The IPv4 address the control plane's runtime endpoint routes to.

    Resolved before the bridge is built rather than inside the probe: the
    firewall rules are chosen from the route to this address, and the probe
    only proves the choice was right.
    """
    parsed = urlsplit(gateway_public_http_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise RuntimeError("worker public gateway URL must be an HTTP origin")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RuntimeError(
            f"worker gateway host {parsed.hostname!r} could not be resolved"
        ) from exc
    if not addresses:
        raise RuntimeError(f"worker gateway host {parsed.hostname!r} has no IPv4 address")
    return str(addresses[0][4][0])


def route_lookup_interface(output: str) -> str:
    """The device `ip route get` chose, from its one-line answer."""
    for line in output.splitlines():
        fields = line.split()
        try:
            index = fields.index("dev")
        except ValueError:
            continue
        if index + 1 < len(fields) and fields[index + 1]:
            return fields[index + 1]
    return ""


def default_route_interface(output: str) -> str:
    for line in output.splitlines():
        fields = line.split()
        if not fields or fields[0] != "default":
            continue
        try:
            index = fields.index("dev")
        except ValueError:
            continue
        if index + 1 < len(fields) and fields[index + 1]:
            return fields[index + 1]
    return ""


@dataclass(slots=True)
class AgentBridgeNetworkBackend:
    ip_allocator: NetworkIpAllocator
    config: AgentBridgeNetworkConfig = field(default_factory=AgentBridgeNetworkConfig)
    system: CommandNetworkSystem = field(default_factory=CommandNetworkSystem)
    assigned_ips: dict[str, str] = field(default_factory=dict)
    egress_counters: WorkerNetworkEgressCounters | None = None
    _capabilities: HostNetworkCapabilities | None = None
    _bridge_ready: bool = False
    _bridge_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _policy_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def capabilities(self) -> HostNetworkCapabilities:
        capabilities = self._capabilities
        if capabilities is None:
            raise RuntimeError("worker bridge network has not been initialized")
        return capabilities

    def initialize(self, gateway_public_http_url: str = "") -> HostNetworkCapabilities:
        gateway_address = (
            _gateway_ipv4_address(gateway_public_http_url) if gateway_public_http_url else ""
        )
        self._ensure_bridge_commands(gateway_address=gateway_address)
        if gateway_public_http_url:
            self._probe_gateway_egress(gateway_public_http_url)
        return self.capabilities

    def _probe_gateway_egress(self, gateway_public_http_url: str) -> None:
        parsed = urlsplit(gateway_public_http_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise RuntimeError("worker public gateway URL must be an HTTP origin")
        origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        # The worker, not just the bridge: the veth pair this name derives is created
        # and unconditionally deleted in the host namespace, so two workers probing
        # under one name tear down each other's in-flight check.
        probe_id = f"netcheck-{self.config.bridge_name}-{self.ip_allocator.worker_id}".rstrip("-")
        reservation = self.ip_allocator.reserve_probe_ip()
        context = ContainerExecutionContext(
            request=ContainerRequestContext(container_id=probe_id),
            startup_kind=WorkerStartupKind.Function,
        )
        script = (
            "import sys;"
            "from urllib.request import urlopen;"
            "response=urlopen(sys.argv[1],timeout=float(sys.argv[2]));"
            "raise SystemExit(0 if 200 <= response.status < 300 else 1)"
        )
        try:
            self._remove_container_resources(probe_id, release_ip=False)
            self.assigned_ips[probe_id] = reservation.ip_address
            self._setup_assigned_network(
                context,
                reservation.ip_address,
                port_bindings=[],
            )
            netns_config_dir = Path(self.config.netns_config_root) / probe_id
            netns_config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                required_container_resolv_conf_source(
                    host_path=self.config.host_resolv_conf_path,
                    fallback_path=self.config.fallback_resolv_conf_path,
                ),
                netns_config_dir / "resolv.conf",
            )
            self.system.run(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.ProbeGatewayEgress,
                    argv=[
                        self.config.ip_binary,
                        "netns",
                        "exec",
                        probe_id,
                        self.config.python_binary,
                        "-c",
                        script,
                        f"{origin}/health",
                        str(self.config.gateway_egress_timeout_seconds),
                    ],
                )
            )
        finally:
            try:
                self._remove_container_resources(probe_id, release_ip=False)
            finally:
                self.ip_allocator.release_probe_ip(reservation)

    def setup_network(
        self,
        context: ContainerExecutionContext,
        *,
        port_bindings: list[PortBinding],
    ) -> ContainerNetworkSetupResult:
        container_id = context.request.container_id
        self._remove_container_resources(container_id, release_ip=False)
        ip_address = self.ip_allocator.reserve_container_ip(container_id)
        self.assigned_ips[container_id] = ip_address
        try:
            result = self._setup_assigned_network(
                context,
                ip_address,
                port_bindings=port_bindings,
            )
            if self.egress_counters is not None:
                self.egress_counters.ensure(
                    container_id,
                    ipv4_interface=self.capabilities.ipv4_interface,
                    ipv6_interface=self.capabilities.ipv6_interface,
                )
            return result
        except Exception as setup_error:
            try:
                self.teardown_network(container_id)
            except Exception as cleanup_error:
                raise ExceptionGroup(
                    f"container {container_id!r} network setup and cleanup failed",
                    [setup_error, cleanup_error],
                ) from cleanup_error
            raise

    def _setup_assigned_network(
        self,
        context: ContainerExecutionContext,
        ip_address: str,
        *,
        port_bindings: list[PortBinding],
    ) -> ContainerNetworkSetupResult:
        container_id = context.request.container_id
        commands: list[NetworkCommand] = []
        policy_operations: list[str] = []
        commands.extend(self._ensure_bridge_commands())
        setup_commands = self._container_setup_commands(
            container_id,
            ip_address,
            port_bindings,
        )
        for command in setup_commands:
            self.system.run(command)
        commands.extend(setup_commands)
        if context.startup_kind is WorkerStartupKind.Sandbox:
            isolation_commands = self._sandbox_control_isolation_commands(
                container_id,
                ip_address,
            )
            for command in isolation_commands:
                self.system.run(command)
            commands.extend(isolation_commands)
        if context.block_network or context.allow_list:
            policy = self.update_network_permissions(
                container_id,
                block_network=context.block_network,
                allow_list=context.allow_list,
            )
            policy_operations = [operation.value for operation in policy.operations]

        veth_host, veth_container = container_veth_names(container_id)
        return ContainerNetworkSetupResult(
            enabled=True,
            identity=ContainerNetworkIdentity(
                container_id=container_id,
                container_ip=ip_address,
                mode=NetworkAddressMode.AgentBridge,
            ),
            namespace_path=str(Path(self.config.host_netns_path) / container_id),
            veth_host=veth_host,
            veth_container=veth_container,
            operations=[
                *(command.operation.value for command in commands),
                *policy_operations,
            ],
            reason="agent bridge network prepared",
        )

    def teardown_network(self, container_id: str) -> None:
        self._remove_container_resources(container_id, release_ip=True)

    def _remove_container_resources(self, container_id: str, *, release_ip: bool) -> None:
        if self.egress_counters is not None:
            self.egress_counters.remove(container_id)
        self._remove_exposed_ports(container_id)
        self._remove_owned_forward_rules(container_id)
        veth_host, _ = container_veth_names(container_id)
        for command in (
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.DeleteVeth,
                argv=[self.config.ip_binary, "link", "delete", veth_host],
                ignore_failure=True,
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.DeleteNamespace,
                argv=[self.config.ip_binary, "netns", "delete", container_id],
                ignore_failure=True,
            ),
        ):
            self.system.run(command)
        shutil.rmtree(
            Path(self.config.netns_config_root) / container_id,
            ignore_errors=True,
        )
        if release_ip:
            self.ip_allocator.release_container_ip(container_id)
        self.assigned_ips.pop(container_id, None)

    def expose_port(self, container_id: str, binding: PortBinding) -> None:
        ip_address = self.container_ip(container_id)
        if not ip_address:
            msg = f"container {container_id} has no assigned bridge IP"
            raise RuntimeError(msg)
        veth_host, _ = container_veth_names(container_id)
        for command in self._port_commands(container_id, veth_host, ip_address, [binding]):
            self.system.run(command)

    def unexpose_port(self, container_id: str, binding: PortBinding) -> None:
        ip_address = self.container_ip(container_id)
        if not ip_address:
            msg = f"container {container_id} has no assigned bridge IP"
            raise RuntimeError(msg)
        veth_host, _ = container_veth_names(container_id)
        for command in self._port_commands(container_id, veth_host, ip_address, [binding]):
            argv = list(command.argv)
            if argv[3] == "-I":
                argv[3] = "-D"
                if len(argv) > 5 and argv[5] == "1":
                    del argv[5]
            else:
                argv[3] = "-D"
            self.system.run(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.UnexposePort,
                    argv=argv,
                    ignore_failure=True,
                )
            )

    def container_ip(self, container_id: str) -> str:
        return self.assigned_ips.get(container_id, "")

    def update_network_permissions(
        self,
        container_id: str,
        *,
        block_network: bool,
        allow_list: list[str],
    ) -> NetworkPolicyUpdateResult:
        self._ensure_bridge_commands()
        capabilities = self.capabilities
        ip_address = self.container_ip(container_id)
        if not ip_address:
            msg = f"container {container_id} has no assigned bridge IP"
            raise RuntimeError(msg)

        plan = plan_network_restriction(
            block_network=block_network,
            allow_list=allow_list,
        )
        info = container_network_info_from_ip(
            container_id,
            ip_address,
            ipv6_enabled=capabilities.ipv6_enabled,
            ipv4_subnet=self.config.subnet,
            ipv6_subnet=self.config.ipv6_subnet,
        )
        with self._policy_lock:
            commands = [
                *self._apply_network_restriction_cleanup(
                    info.container_ip,
                    self.config.iptables_binary,
                )
            ]
            if capabilities.ipv6_enabled and info.container_ipv6:
                commands.extend(
                    self._apply_network_restriction_cleanup(
                        info.container_ipv6,
                        self.config.ip6tables_binary,
                    )
                )
            policy_commands = self._network_policy_commands(plan, info)
            for command in policy_commands:
                self.system.run(command)
            commands.extend(policy_commands)
        return NetworkPolicyUpdateResult(
            container_id=container_id,
            mode=plan.mode,
            container_ip=info.container_ip,
            container_ipv6=info.container_ipv6,
            operations=[command.operation for command in commands],
        )

    def _ensure_bridge_commands(self, *, gateway_address: str = "") -> list[NetworkCommand]:
        with self._bridge_lock:
            if self._bridge_ready:
                return []
            token = self.ip_allocator.acquire_network_lock()
            try:
                capabilities = self.system.discover_host_capabilities(
                    self.config,
                    gateway_address=gateway_address,
                )
                commands = self._ensure_bridge_link()
                bridge_commands = self._bridge_commands(capabilities)
                for command in bridge_commands:
                    self.system.run(command)
                commands.extend(bridge_commands)
                commands.extend(self._ensure_base_firewall_rules(capabilities))
                self._capabilities = capabilities
                self._bridge_ready = True
                return commands
            finally:
                self.ip_allocator.release_network_lock(token)

    def _container_setup_commands(
        self,
        container_id: str,
        ip_address: str,
        port_bindings: list[PortBinding],
    ) -> list[NetworkCommand]:
        veth_host, veth_container = container_veth_names(container_id)
        commands = [
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.CreateVethPair,
                argv=[
                    self.config.ip_binary,
                    "link",
                    "add",
                    veth_host,
                    "type",
                    "veth",
                    "peer",
                    "name",
                    veth_container,
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.AttachHostVeth,
                argv=[
                    self.config.ip_binary,
                    "link",
                    "set",
                    veth_host,
                    "master",
                    self.config.bridge_name,
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.AttachHostVeth,
                argv=[self.config.ip_binary, "link", "set", veth_host, "up"],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.CreateNamespace,
                argv=[self.config.ip_binary, "netns", "add", container_id],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.MoveContainerVeth,
                argv=[
                    self.config.ip_binary,
                    "link",
                    "set",
                    veth_container,
                    "netns",
                    container_id,
                ],
            ),
            *self._namespace_commands(container_id, veth_container, ip_address),
            *self._port_commands(container_id, veth_host, ip_address, port_bindings),
        ]
        return commands

    def _bridge_commands(
        self,
        capabilities: HostNetworkCapabilities,
    ) -> list[NetworkCommand]:
        commands = [
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.EnableForwarding,
                argv=[self.config.sysctl_binary, "-w", "net.ipv4.ip_forward=1"],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.EnsureBridge,
                argv=[
                    self.config.ip_binary,
                    "addr",
                    "replace",
                    self.config.gateway_cidr,
                    "dev",
                    self.config.bridge_name,
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.EnsureBridge,
                argv=[self.config.ip_binary, "link", "set", self.config.bridge_name, "up"],
            ),
        ]
        if capabilities.ipv6_enabled:
            commands.extend(
                [
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.EnableForwarding,
                        argv=[
                            self.config.sysctl_binary,
                            "-w",
                            "net.ipv6.conf.all.forwarding=1",
                        ],
                    ),
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.EnsureBridge,
                        argv=[
                            self.config.ip_binary,
                            "-6",
                            "addr",
                            "replace",
                            self.config.gateway_ipv6_cidr,
                            "dev",
                            self.config.bridge_name,
                        ],
                    ),
                ]
            )
        return commands

    def _ensure_bridge_link(self) -> list[NetworkCommand]:
        inspect = NetworkCommand(
            operation=AgentBridgeNetworkOperation.InspectBridge,
            argv=[
                self.config.ip_binary,
                "link",
                "show",
                "dev",
                self.config.bridge_name,
            ],
            ignore_failure=True,
        )
        inspect_result = self.system.run(inspect)
        commands = [inspect]
        if not inspect_result.ok:
            create = NetworkCommand(
                operation=AgentBridgeNetworkOperation.EnsureBridge,
                argv=[
                    self.config.ip_binary,
                    "link",
                    "add",
                    self.config.bridge_name,
                    "type",
                    "bridge",
                ],
            )
            self.system.run(create)
            commands.append(create)
            return commands
        commands.append(self._require_bridge_owns_subnet())
        return commands

    def _container_ipv6(self, ip_address: str) -> str:
        """The v6 address matching a v4 one, on this backend's own bridge.

        Both subnets have to come from the same place: the v6 address is the v4 host
        offset rebased, so pairing a configured v4 subnet with the default v6 one
        rejects every address the allocator issues.
        """

        return container_ipv6_address(
            ip_address,
            ipv4_subnet=self.config.subnet,
            ipv6_subnet=self.config.ipv6_subnet,
        )

    def _require_bridge_owns_subnet(self) -> NetworkCommand:
        """Refuse a bridge that is already fronting a different network.

        Two agents on one host each address containers inside their own control-plane
        scope, so a bridge name used twice is two allocators issuing the same address
        with nothing between them. The gateway assignment would otherwise be taken over
        in silence, by whichever agent restarted last.
        """

        addresses = NetworkCommand(
            operation=AgentBridgeNetworkOperation.InspectBridge,
            argv=[self.config.ip_binary, "-4", "addr", "show", "dev", self.config.bridge_name],
            ignore_failure=True,
        )
        result = self.system.run(addresses)
        network = ipaddress.ip_network(self.config.subnet, strict=False)
        for token in (result.stdout or "").split():
            if "/" not in token:
                continue
            try:
                existing = ipaddress.ip_interface(token)
            except ValueError:
                continue
            if existing.network.version != network.version:
                continue
            if existing.ip not in network:
                msg = (
                    f"bridge {self.config.bridge_name} already carries {existing}, which is "
                    f"outside {self.config.subnet}; another agent on this host is using the "
                    f"same bridge name for a different network"
                )
                raise RuntimeError(msg)
        return addresses

    def _ensure_base_firewall_rules(
        self,
        capabilities: HostNetworkCapabilities,
    ) -> list[NetworkCommand]:
        commands: list[NetworkCommand] = []
        # Before NAT and tenant allowlists: a sandbox must never read node
        # metadata or bootstrap credentials through the host's source address.
        destinations = [(self.config.iptables_binary, "169.254.0.0/16")]
        if capabilities.ipv6_enabled:
            destinations.extend(
                (self.config.ip6tables_binary, address)
                for address in (
                    "fd00:ec2::254/128",
                    "fd20:ce::254/128",
                    "fd00:a9fe:a9fe::1/128",
                    "fe80::a9fe:a9fe/128",
                )
            )
        for binary, destination in destinations:
            commands.extend(
                self._ensure_firewall_rule(
                    binary=binary,
                    table="raw",
                    chain="PREROUTING",
                    rule=["-i", self.config.bridge_name, "-d", destination, "-j", "DROP"],
                    operation=AgentBridgeNetworkOperation.BlockProviderMetadata,
                    insert=True,
                )
            )
        commands.extend(
            self._ensure_firewall_rule(
                binary=self.config.iptables_binary,
                table="nat",
                chain="POSTROUTING",
                rule=[
                    "-s",
                    self.config.subnet,
                    "-o",
                    capabilities.ipv4_interface,
                    "-j",
                    "MASQUERADE",
                ],
                operation=AgentBridgeNetworkOperation.EnableMasquerade,
            )
        )
        commands.extend(
            self._ensure_forwarding_rules(
                binary=self.config.iptables_binary,
                egress_interface=capabilities.ipv4_interface,
            )
        )
        if capabilities.gateway_interface:
            # Masqueraded to the host's own address on that interface. A WireGuard
            # server admits the node's single tunnel address and nothing behind
            # it, so a container's packet has to leave as the node or it is
            # dropped at the far end without a reply to explain why.
            commands.extend(
                self._ensure_firewall_rule(
                    binary=self.config.iptables_binary,
                    table="nat",
                    chain="POSTROUTING",
                    rule=[
                        "-s",
                        self.config.subnet,
                        "-o",
                        capabilities.gateway_interface,
                        "-j",
                        "MASQUERADE",
                    ],
                    operation=AgentBridgeNetworkOperation.EnableMasquerade,
                )
            )
            commands.extend(
                self._ensure_forwarding_rules(
                    binary=self.config.iptables_binary,
                    egress_interface=capabilities.gateway_interface,
                )
            )
        if capabilities.ipv6_enabled:
            commands.extend(
                self._ensure_firewall_rule(
                    binary=self.config.ip6tables_binary,
                    table="nat",
                    chain="POSTROUTING",
                    rule=[
                        "-s",
                        self.config.ipv6_subnet,
                        "-o",
                        capabilities.ipv6_interface,
                        "-j",
                        "MASQUERADE",
                    ],
                    operation=AgentBridgeNetworkOperation.EnableMasquerade,
                )
            )
            commands.extend(
                self._ensure_forwarding_rules(
                    binary=self.config.ip6tables_binary,
                    egress_interface=capabilities.ipv6_interface,
                )
            )
        return commands

    def _ensure_forwarding_rules(
        self,
        *,
        binary: str,
        egress_interface: str,
    ) -> list[NetworkCommand]:
        commands: list[NetworkCommand] = []
        for rule in (
            [
                "-i",
                self.config.bridge_name,
                "-o",
                egress_interface,
                "-j",
                "ACCEPT",
            ],
            [
                "-i",
                egress_interface,
                "-o",
                self.config.bridge_name,
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ],
        ):
            commands.extend(
                self._ensure_firewall_rule(
                    binary=binary,
                    table="filter",
                    chain="FORWARD",
                    rule=rule,
                    operation=AgentBridgeNetworkOperation.AllowForwarding,
                    insert=True,
                )
            )
        return commands

    def _ensure_firewall_rule(
        self,
        *,
        binary: str,
        table: str,
        chain: str,
        rule: list[str],
        operation: AgentBridgeNetworkOperation,
        insert: bool = False,
    ) -> list[NetworkCommand]:
        check = NetworkCommand(
            operation=AgentBridgeNetworkOperation.CheckFirewallRule,
            argv=[binary, "-t", table, "-C", chain, *rule],
            ignore_failure=True,
        )
        result = self.system.run(check)
        commands = [check]
        if result.ok:
            return commands
        add = NetworkCommand(
            operation=operation,
            argv=[
                binary,
                "-t",
                table,
                "-I" if insert else "-A",
                chain,
                *(["1"] if insert else []),
                *rule,
            ],
        )
        self.system.run(add)
        commands.append(add)
        return commands

    def _namespace_commands(
        self,
        container_id: str,
        veth_container: str,
        ip_address: str,
    ) -> list[NetworkCommand]:
        capabilities = self.capabilities
        commands = [
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                argv=[
                    self.config.ip_binary,
                    "netns",
                    "exec",
                    container_id,
                    self.config.ip_binary,
                    "link",
                    "set",
                    "lo",
                    "up",
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                argv=[
                    self.config.ip_binary,
                    "netns",
                    "exec",
                    container_id,
                    self.config.ip_binary,
                    "addr",
                    "add",
                    self.config.container_cidr(ip_address),
                    "dev",
                    veth_container,
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                argv=[
                    self.config.ip_binary,
                    "netns",
                    "exec",
                    container_id,
                    self.config.ip_binary,
                    "link",
                    "set",
                    veth_container,
                    "up",
                ],
            ),
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                argv=[
                    self.config.ip_binary,
                    "netns",
                    "exec",
                    container_id,
                    self.config.ip_binary,
                    "route",
                    "add",
                    "default",
                    "via",
                    self.config.gateway,
                ],
            ),
        ]
        if capabilities.ipv6_enabled:
            commands.extend(
                [
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                        argv=[
                            self.config.ip_binary,
                            "netns",
                            "exec",
                            container_id,
                            self.config.ip_binary,
                            "-6",
                            "addr",
                            "replace",
                            f"{self._container_ipv6(ip_address)}/64",
                            "dev",
                            veth_container,
                        ],
                    ),
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.ConfigureNamespace,
                        argv=[
                            self.config.ip_binary,
                            "netns",
                            "exec",
                            container_id,
                            self.config.ip_binary,
                            "-6",
                            "route",
                            "replace",
                            "default",
                            "via",
                            self.config.gateway_ipv6,
                        ],
                    ),
                ]
            )
        return commands

    def _port_commands(
        self,
        container_id: str,
        veth_host: str,
        ip_address: str,
        port_bindings: list[PortBinding],
    ) -> list[NetworkCommand]:
        comment = container_network_comment(veth_host, container_id)
        commands: list[NetworkCommand] = []
        for binding in port_bindings:
            commands.extend(
                [
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.ExposePort,
                        argv=[
                            self.config.iptables_binary,
                            "-t",
                            "nat",
                            "-A",
                            "PREROUTING",
                            "-p",
                            "tcp",
                            "--dport",
                            str(binding.host_port),
                            "-j",
                            "DNAT",
                            "--to-destination",
                            f"{ip_address}:{binding.container_port}",
                            "-m",
                            "comment",
                            "--comment",
                            comment,
                        ],
                    ),
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.AllowForwarding,
                        argv=[
                            self.config.iptables_binary,
                            "-t",
                            "filter",
                            "-I",
                            "FORWARD",
                            "1",
                            "-d",
                            ip_address,
                            "-p",
                            "tcp",
                            "--dport",
                            str(binding.container_port),
                            "-o",
                            self.config.bridge_name,
                            "-j",
                            "ACCEPT",
                            "-m",
                            "comment",
                            "--comment",
                            comment,
                        ],
                    ),
                ]
            )
        return commands

    def _sandbox_control_isolation_commands(
        self,
        container_id: str,
        ip_address: str,
    ) -> list[NetworkCommand]:
        veth_host, _ = container_veth_names(container_id)
        comment = container_network_comment(veth_host, container_id)
        commands = [
            self._sandbox_control_isolation_command(
                self.config.iptables_binary,
                ip_address,
                comment,
            )
        ]
        if self.capabilities.ipv6_enabled:
            commands.append(
                self._sandbox_control_isolation_command(
                    self.config.ip6tables_binary,
                    self._container_ipv6(ip_address),
                    comment,
                )
            )
        return commands

    def _sandbox_control_isolation_command(
        self,
        binary: str,
        destination: str,
        comment: str,
    ) -> NetworkCommand:
        return NetworkCommand(
            operation=AgentBridgeNetworkOperation.IsolateSandboxControl,
            argv=[
                binary,
                "-t",
                "filter",
                "-I",
                "FORWARD",
                "1",
                "-d",
                destination,
                "-p",
                "tcp",
                "--dport",
                str(WORKER_SANDBOX_PROCESS_MANAGER_PORT),
                "-j",
                "DROP",
                "-m",
                "comment",
                "--comment",
                comment,
            ],
        )

    def _remove_owned_forward_rules(self, container_id: str) -> None:
        for binary in (self.config.iptables_binary, self.config.ip6tables_binary):
            if binary == self.config.ip6tables_binary and not self.config.enable_ipv6:
                continue
            list_command = NetworkCommand(
                operation=AgentBridgeNetworkOperation.ListSandboxControlIsolation,
                argv=[binary, "-t", "filter", "-S", "FORWARD"],
                ignore_failure=True,
            )
            result = self.system.run(list_command)
            for rule in result.stdout.splitlines():
                rule_container_id, found = container_id_from_iptables_rule(rule)
                fields = iptables_rule_fields(rule)
                if (
                    not found
                    or rule_container_id != container_id
                    or len(fields) < 3
                    or fields[:2] != ["-A", "FORWARD"]
                ):
                    continue
                self.system.run(
                    NetworkCommand(
                        operation=AgentBridgeNetworkOperation.DeleteOwnedForwardRule,
                        argv=[binary, "-t", "filter", "-D", "FORWARD", *fields[2:]],
                        ignore_failure=True,
                    )
                )

    def _remove_exposed_ports(self, container_id: str) -> None:
        list_command = NetworkCommand(
            operation=AgentBridgeNetworkOperation.ListExposedPorts,
            argv=[self.config.iptables_binary, "-t", "nat", "-S", "PREROUTING"],
            ignore_failure=True,
        )
        result = self.system.run(list_command)
        for rule in result.stdout.splitlines():
            rule_container_id, found = container_id_from_iptables_rule(rule)
            fields = iptables_rule_fields(rule)
            if (
                not found
                or rule_container_id != container_id
                or len(fields) < 3
                or fields[0] != "-A"
                or fields[1] != "PREROUTING"
            ):
                continue
            self.system.run(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.DeleteExposedPort,
                    argv=[
                        self.config.iptables_binary,
                        "-t",
                        "nat",
                        "-D",
                        "PREROUTING",
                        *fields[2:],
                    ],
                    ignore_failure=True,
                )
            )

    def _network_policy_commands(
        self,
        plan: NetworkRestrictionPlan,
        info: ContainerNetworkInfo,
    ) -> list[NetworkCommand]:
        if not plan.apply:
            return []
        if plan.mode is NetworkRestrictionMode.Allowlist:
            return [
                *self._network_block_commands(info),
                *self._network_allow_list_commands(info, plan),
            ]
        if plan.mode is NetworkRestrictionMode.Block:
            return self._network_block_commands(info)
        return []

    def _apply_network_restriction_cleanup(
        self,
        ip_address: str,
        binary: str,
    ) -> list[NetworkCommand]:
        list_command = NetworkCommand(
            operation=AgentBridgeNetworkOperation.ListNetworkRestrictions,
            argv=[binary, "-t", "filter", "-S", "FORWARD"],
        )
        result = self.system.run(list_command)
        commands = [list_command]
        for command in self._network_restriction_delete_commands(
            result.stdout,
            ip_address,
            binary,
        ):
            self.system.run(command)
            commands.append(command)
        return commands

    def _network_restriction_delete_commands(
        self,
        rules: str,
        ip_address: str,
        binary: str,
    ) -> list[NetworkCommand]:
        commands: list[NetworkCommand] = []
        for rule in rules.splitlines():
            if iptables_rule_target(rule) not in {"DROP", "ACCEPT"}:
                continue
            if not iptables_rule_matches_source_ip(rule, ip_address):
                continue
            fields = iptables_rule_fields(rule)
            if len(fields) < 3 or fields[0] != "-A" or fields[1] != "FORWARD":
                continue
            commands.append(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.DeleteNetworkRestriction,
                    argv=[binary, "-t", "filter", "-D", "FORWARD", *fields[2:]],
                )
            )
        return commands

    def _network_block_commands(self, info: ContainerNetworkInfo) -> list[NetworkCommand]:
        commands = [
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.BlockNetwork,
                argv=[
                    self.config.iptables_binary,
                    "-t",
                    "filter",
                    "-I",
                    "FORWARD",
                    "1",
                    "-s",
                    info.container_ip,
                    "-m",
                    "conntrack",
                    "!",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "DROP",
                    "-m",
                    "comment",
                    "--comment",
                    info.comment,
                ],
            )
        ]
        if self.capabilities.ipv6_enabled and info.container_ipv6:
            commands.append(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.BlockNetwork,
                    argv=[
                        self.config.ip6tables_binary,
                        "-t",
                        "filter",
                        "-I",
                        "FORWARD",
                        "1",
                        "-s",
                        info.container_ipv6,
                        "-m",
                        "conntrack",
                        "!",
                        "--ctstate",
                        "ESTABLISHED,RELATED",
                        "-j",
                        "DROP",
                        "-m",
                        "comment",
                        "--comment",
                        info.comment,
                    ],
                )
            )
        return commands

    def _network_allow_list_commands(
        self,
        info: ContainerNetworkInfo,
        plan: NetworkRestrictionPlan,
    ) -> list[NetworkCommand]:
        commands = [
            NetworkCommand(
                operation=AgentBridgeNetworkOperation.AllowNetworkDestination,
                argv=[
                    self.config.iptables_binary,
                    "-t",
                    "filter",
                    "-I",
                    "FORWARD",
                    "1",
                    "-s",
                    info.container_ip,
                    "-d",
                    cidr,
                    "-j",
                    "ACCEPT",
                    "-m",
                    "comment",
                    "--comment",
                    info.comment,
                ],
            )
            for cidr in plan.ipv4_allow_list
        ]
        if self.capabilities.ipv6_enabled and info.container_ipv6:
            commands.extend(
                NetworkCommand(
                    operation=AgentBridgeNetworkOperation.AllowNetworkDestination,
                    argv=[
                        self.config.ip6tables_binary,
                        "-t",
                        "filter",
                        "-I",
                        "FORWARD",
                        "1",
                        "-s",
                        info.container_ipv6,
                        "-d",
                        cidr,
                        "-j",
                        "ACCEPT",
                        "-m",
                        "comment",
                        "--comment",
                        info.comment,
                    ],
                )
                for cidr in plan.ipv6_allow_list
            )
        return commands

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_network
from threading import RLock
from time import monotonic

from foundation.process import ProcessResult, run_process
from pydantic import Field
from shared.contracts import ContractModel
from shared.http.worker_network import WorkerEgressPolicy
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

from worker.execution import container_veth_names

# These ranges cannot represent customer internet destinations. Routing to the
# gateway's tunnel is excluded separately by matching the actual internet uplink.
_INTERNAL = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/3",
    "::/128",
    "::1/128",
    "::ffff:0:0/96",
    "64:ff9b:1::/48",
    "100::/64",
    "2001:db8::/32",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)
_COUNTER_COMMENT = "lazycloud-internet-ip-bytes"
LOGGER = logging.getLogger(__name__)


class NetworkEgressCounterSample(ContractModel):
    total_bytes: int = Field(ge=0)
    policy_digest: str


@dataclass(slots=True)
class WorkerNetworkEgressCounters:
    load_policy: Callable[[], WorkerEgressPolicy]
    run_command: Callable[[list[str]], ProcessResult] = run_process
    ipv4_binary: str = "iptables"
    ipv6_binary: str = "ip6tables"
    _policy: WorkerEgressPolicy | None = None
    _verified_monotonic: float = 0
    _continuity: int = 0
    _interfaces: dict[str, tuple[str, str]] = field(default_factory=dict)
    _destinations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def ensure(self, container_id: str, *, ipv4_interface: str, ipv6_interface: str) -> None:
        with self._lock:
            policy = self._refresh_policy()
            self._interfaces[container_id] = (ipv4_interface, ipv6_interface)
            if policy.billing_owner is not UsageBillingOwner.PlatformFleet:
                return
            if policy.routes is None:
                raise RuntimeError("platform egress policy has no route evidence")
            for version, binary, interface in self._families(container_id):
                if version not in policy.routes.verified_ip_versions:
                    LOGGER.warning(
                        "IPv%s internet egress is unbilled without provider route evidence", version
                    )
                chain = self._chain(container_id)
                if not self._command(binary, ["-S", chain], required=False).ok:
                    self._command(binary, ["-N", chain])
                terminal = ["-m", "comment", "--comment", _COUNTER_COMMENT, "-j", "RETURN"]
                if not self._command(binary, ["-C", chain, *terminal], required=False).ok:
                    self._command(binary, ["-A", chain, *terminal])
                self._replace_exclusions(container_id, version, binary, policy)
                jump = self._jump(container_id, interface)
                if not self._command(binary, ["-C", "POSTROUTING", *jump], required=False).ok:
                    self._command(binary, ["-A", "POSTROUTING", *jump])

    def sample(self, container_id: str) -> NetworkEgressCounterSample:
        with self._lock:
            try:
                return self._sample(container_id)
            except Exception:
                # The metrics publication can fail too, leaving its prior reading
                # intact. A generation change keeps that stale reading from
                # pricing the interval whose route evidence was unavailable.
                self._continuity += 1
                raise

    def _sample(self, container_id: str) -> NetworkEgressCounterSample:
        policy = self._refresh_policy()
        if policy.billing_owner is not UsageBillingOwner.PlatformFleet:
            return NetworkEgressCounterSample(
                total_bytes=0, policy_digest=policy.billing_owner.value
            )
        if container_id not in self._interfaces:
            raise RuntimeError("container has no owned internet egress counters")
        if policy.routes is None:
            raise RuntimeError("platform egress policy has no route evidence")
        total = 0
        for version, binary, _ in self._families(container_id):
            if version not in policy.routes.verified_ip_versions:
                continue
            self._replace_exclusions(container_id, version, binary, policy)
            result = self._command(binary, ["-L", self._chain(container_id), "-n", "-v", "-x"])
            counters = [
                line.split() for line in result.stdout.splitlines() if _COUNTER_COMMENT in line
            ]
            if len(counters) != 1 or len(counters[0]) < 3 or counters[0][2] != "RETURN":
                raise RuntimeError("owned internet egress counter is missing or ambiguous")
            total += int(counters[0][1])
        return NetworkEgressCounterSample(
            total_bytes=total,
            policy_digest=f"{self._continuity}:"
            + sha256(policy.routes.model_dump_json().encode()).hexdigest(),
        )

    def remove(self, container_id: str) -> None:
        with self._lock:
            for binary in (self.ipv4_binary, self.ipv6_binary):
                chain = self._chain(container_id)
                if self._command(binary, ["-S", chain], required=False).ok:
                    rules = self._command(binary, ["-S", "POSTROUTING"])
                    for line in rules.stdout.splitlines():
                        fields = shlex.split(line)
                        if len(fields) < 4 or fields[:3] != ["-A", "POSTROUTING", "-o"]:
                            continue
                        jump = self._jump(container_id, fields[3])
                        if fields[2:] == jump:
                            self._command(binary, ["-D", "POSTROUTING", *jump])
                    self._command(binary, ["-F", chain])
                    self._command(binary, ["-X", chain])
            self._interfaces.pop(container_id, None)
            self._destinations.pop(container_id + ":4", None)
            self._destinations.pop(container_id + ":6", None)

    def _refresh_policy(self) -> WorkerEgressPolicy:
        if self._policy is None or monotonic() - self._verified_monotonic >= 60:
            policy = self.load_policy()
            age = (utc_now() - policy.verified_at).total_seconds()
            if age < -5 or age > 60:
                raise RuntimeError("worker egress route evidence is not current")
            self._policy = policy
            self._verified_monotonic = monotonic()
        return self._policy

    def _replace_exclusions(
        self, container_id: str, version: int, binary: str, policy: WorkerEgressPolicy
    ) -> None:
        key = f"{container_id}:{version}"
        if policy.routes is None:
            raise RuntimeError("platform egress policy has no route evidence")
        destinations = tuple(
            sorted(
                {
                    cidr
                    for cidr in (*_INTERNAL, *policy.routes.excluded_destinations)
                    if ip_network(cidr).version == version
                }
            )
        )
        previous = self._destinations.get(key, ())
        if previous == destinations:
            return
        chain = self._chain(container_id)
        # Insert before removing so a route change cannot temporarily charge an
        # exempt destination. The terminal counter survives policy refreshes.
        for cidr in destinations:
            rule = ["-d", cidr, "-j", "RETURN"]
            if not self._command(binary, ["-C", chain, *rule], required=False).ok:
                self._command(binary, ["-I", chain, "1", *rule])
        for cidr in set(previous) - set(destinations):
            self._command(binary, ["-D", chain, "-d", cidr, "-j", "RETURN"])
        self._destinations[key] = destinations

    def _families(self, container_id: str) -> tuple[tuple[int, str, str], ...]:
        ipv4, ipv6 = self._interfaces[container_id]
        return tuple(
            (version, binary, interface)
            for version, binary, interface in (
                (4, self.ipv4_binary, ipv4),
                (6, self.ipv6_binary, ipv6),
            )
            if interface
        )

    @staticmethod
    def _chain(container_id: str) -> str:
        return "LZY_EG_" + sha256(container_id.encode()).hexdigest()[:20]

    def _jump(self, container_id: str, interface: str) -> list[str]:
        host_veth, _ = container_veth_names(container_id)
        return [
            "-o",
            interface,
            "-m",
            "physdev",
            "--physdev-in",
            host_veth,
            "-j",
            self._chain(container_id),
        ]

    def _command(self, binary: str, args: list[str], *, required: bool = True) -> ProcessResult:
        result = self.run_command([binary, "-w", "5", "-t", "mangle", *args])
        if required and not result.ok:
            raise RuntimeError(result.stderr or "worker egress counter operation failed")
        return result

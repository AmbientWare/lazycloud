from __future__ import annotations

import ipaddress
import time

from shared.scheduling import NetworkIpMutationAction, NetworkIpMutationPlan

from scheduler.state import RedisWorkerNetworkIpRepository, SchedulerRepositoryError


def reserve_container_ip(
    repository: RedisWorkerNetworkIpRepository,
    *,
    network_prefix: str,
    container_id: str,
    subnet: str,
) -> NetworkIpMutationPlan:
    network = ipaddress.IPv4Network(subnet, strict=False)
    if network.prefixlen < 16 or network.prefixlen > 30:
        raise ValueError("container subnet must have a prefix length between 16 and 30")
    deadline = time.monotonic() + 2
    while True:
        lock = repository.set_network_lock(network_prefix, ttl_seconds=30, retries=0)
        if lock.acquired and lock.token:
            break
        if time.monotonic() >= deadline:
            raise SchedulerRepositoryError("container address allocation lock is unavailable")
        time.sleep(0.01)
    try:
        existing = repository.get_container_ip(network_prefix, container_id)
        if existing is not None:
            if ipaddress.IPv4Address(existing) not in network:
                raise SchedulerRepositoryError("container address belongs to a different subnet")
            return NetworkIpMutationPlan(
                action=NetworkIpMutationAction.Noop,
                container_id=container_id,
                ip_address=existing,
                reason="container address is already reserved",
            )
        assigned = {item.ip_address for item in repository.list_assignments(network_prefix)}
        hosts = network.hosts()
        next(hosts)  # The first host is the bridge gateway.
        for address in hosts:
            candidate = str(address)
            if candidate not in assigned:
                return repository.set_container_ip(network_prefix, container_id, candidate)
        raise SchedulerRepositoryError("container subnet has no available addresses")
    finally:
        released = repository.remove_network_lock(network_prefix, lock.token)
        if not released.released:
            raise SchedulerRepositoryError("container address allocation lock was not released")

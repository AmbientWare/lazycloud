from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from scheduler.network_allocation import reserve_container_ip
from scheduler.state import RedisWorkerNetworkIpRepository
from tests.real_redis import RealRedisActors


def test_concurrent_address_reservations_are_unique_and_retry_keeps_ownership(
    real_redis_actors: RealRedisActors,
) -> None:
    repository = RedisWorkerNetworkIpRepository(real_redis_actors.client())

    def reserve(index: int) -> str:
        return reserve_container_ip(
            repository,
            network_prefix="concurrent-allocation",
            container_id=f"container-{index}",
            subnet="10.200.0.0/28",
        ).ip_address

    with ThreadPoolExecutor(max_workers=8) as executor:
        addresses = list(executor.map(reserve, range(8)))
        repeated = list(executor.map(reserve, range(8)))

    assert len(set(addresses)) == 8
    assert "10.200.0.1" not in addresses
    assert repeated == addresses
    assert {
        item.ip_address for item in repository.list_assignments("concurrent-allocation")
    } == set(addresses)

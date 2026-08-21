from __future__ import annotations

import pytest
from pydantic import ValidationError
from worker.execution import MIB, ContainerResourceRequest, plan_oci_linux_resources


def test_the_request_is_reserved_and_the_ceiling_sits_above_it() -> None:
    """A container is guaranteed what it asked for and may grow past it.

    The reservation is what protects it when the node comes under memory
    pressure; the limit is only where the kernel stops it. Collapsing the two
    would pin a default function to its request on an otherwise idle worker.
    """
    resources = plan_oci_linux_resources(
        ContainerResourceRequest(cpu_millicores=125, memory_mib=128)
    )

    assert resources.memory is not None
    assert resources.memory.reservation_bytes == 128 * MIB
    assert resources.memory.limit_bytes > resources.memory.reservation_bytes
    # Shares track the request, so contention divides the node in the proportion
    # each container asked for, while quota leaves idle capacity usable.
    assert resources.cpu.quota > 125 * resources.cpu.period // 1000


def test_a_ceiling_the_author_named_is_the_one_applied() -> None:
    resources = plan_oci_linux_resources(
        ContainerResourceRequest(
            cpu_millicores=1_000,
            memory_mib=1_024,
            cpu_limit_millicores=2_000,
            memory_limit_mib=2_048,
        )
    )

    assert resources.memory is not None
    assert resources.memory.limit_bytes == 2_048 * MIB
    assert resources.cpu.quota == 2_000 * resources.cpu.period // 1000


def test_a_ceiling_below_the_request_is_refused() -> None:
    """The scheduler promised the request. The kernel would kill it for taking it."""
    with pytest.raises(ValidationError):
        ContainerResourceRequest(
            cpu_millicores=1_000,
            memory_mib=1_024,
            memory_limit_mib=512,
        )


def test_the_reservation_the_throttle_and_the_wall_stay_in_order() -> None:
    """`low <= high <= max`, in every shape the clamp can produce.

    A throttle above the wall is unreachable, so the container is killed having
    never been slowed. A wall below the reservation kills it inside what it was
    promised. Both were reachable while only half of this was asserted.
    """
    for request_mib, node_mib in (
        (4096, 8192),
        (1024, 2048),
        (512, 1024),
        (128, 4096),
        (14894, 15974),
        (1024, 0),
    ):
        resources = plan_oci_linux_resources(
            ContainerResourceRequest(
                cpu_millicores=1_000,
                memory_mib=request_mib,
                node_memory_mib=node_mib,
            )
        )
        assert resources.memory is not None
        low = resources.memory.reservation_bytes
        high = int(resources.deferred["memory.high"])
        wall = resources.memory.limit_bytes
        assert low <= high <= wall, f"request={request_mib} node={node_mib}"


def test_a_ceiling_never_exceeds_the_machine_it_runs_on() -> None:
    """A container has to be able to reach its own ceiling for it to stop it.

    Above the node's size it never can, so the machine runs out first and the
    kernel's global OOM killer resolves the shortage instead — by resident size,
    which is the one thing that has never read anyone's reservation.
    """
    on_a_small_node = plan_oci_linux_resources(
        ContainerResourceRequest(
            cpu_millicores=1_000,
            memory_mib=4_096,
            node_cpu_millicores=2_000,
            node_memory_mib=8_192,
        )
    )

    assert on_a_small_node.memory is not None
    assert on_a_small_node.memory.limit_bytes <= 8_192 * MIB
    assert on_a_small_node.cpu.quota <= 2_000 * on_a_small_node.cpu.period // 1000


def test_a_container_can_reclaim_rather_than_die_at_its_ceiling() -> None:
    """`memory.high` throttles, `memory.max` kills, and swap is what separates them.

    Without somewhere to reclaim to, a cgroup of anonymous pages does not slow
    down at the throttle, it stalls. Granting swap equal to the limit, as this
    did before, is granting none: OCI states memory-plus-swap.
    """
    resources = plan_oci_linux_resources(
        ContainerResourceRequest(cpu_millicores=1_000, memory_mib=1_024)
    )

    assert resources.memory is not None
    high = int(resources.deferred["memory.high"])
    assert resources.memory.reservation_bytes < high <= resources.memory.limit_bytes
    assert resources.memory.swap_bytes > resources.memory.limit_bytes

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from shared.container_requests import DEFAULT_CONTAINER_OOM_THRESHOLD_PERCENT
from worker.execution import MIB, ContainerResourceRequest, plan_oci_linux_resources
from worker.runtime_config import (
    build_base_oci_config,
    container_cgroup_path,
    parse_proc_cgroup_path,
)


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


def test_a_container_is_never_killed_inside_its_reservation() -> None:
    """The sandbox watcher kills at a percentage of the wall, so the wall matters.

    Ordering alone proves nothing here: the floors make `low <= high <= max` true
    by construction. What was actually reachable is a wall so close to the
    reservation that ninety-five per cent of it lands underneath, and on a node
    only slightly larger than the request that was every placement — a tenant
    promised 4096 MiB dying at 3979, never throttled, inside its own guarantee.

    The rows below are the ratios that produced it.
    """
    for request_mib, node_mib in (
        (4096, 4608),
        (14894, 15974),
        (1024, 1126),
        (4096, 8192),
        (128, 4096),
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
        where = f"request={request_mib} node={node_mib}"
        assert low <= high <= wall, where
        kills_at = wall * DEFAULT_CONTAINER_OOM_THRESHOLD_PERCENT / 100
        assert kills_at > low, where


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


def test_a_container_gets_no_cgroup_when_the_parent_cannot_bound_it(tmp_path: Path) -> None:
    """An undelegated parent creates children with no memory files at all.

    Naming one anyway hands the runtime a path that looks like enforcement and
    holds none: `memory.max` and `memory.low` are absent, the deferred writes miss
    files that were never there, and the container runs unbounded while the log
    says only that eviction is off. Refusing the path leaves the runtime to place
    it, which is worse but visibly so.
    """
    relative = parse_proc_cgroup_path(Path("/proc/self/cgroup").read_text(encoding="utf-8"))
    worker = tmp_path / relative.lstrip("/")
    worker.mkdir(parents=True)
    # The parent is a real cgroup either way; what differs is what it hands down.
    (worker / "memory.pressure").write_text("full avg10=0.00\n")
    control = worker / "cgroup.subtree_control"

    control.write_text("cpu pids\n")
    assert container_cgroup_path("container-abc", root=str(tmp_path)) == ""

    control.write_text("cpu memory pids\n")
    path = container_cgroup_path("container-abc", root=str(tmp_path))
    assert path.endswith("/container-abc")
    # Underneath the worker's own cgroup, so the slot the agent gave it bounds
    # the container too, and the container's growth reaches the pressure reading.
    assert Path(tmp_path, path.lstrip("/")).parent == worker


def test_a_spec_built_without_a_container_names_no_cgroup() -> None:
    """Better to leave the runtime to choose than to place one at the root."""
    linux = build_base_oci_config()["linux"]
    assert isinstance(linux, dict)
    assert "cgroupsPath" not in linux

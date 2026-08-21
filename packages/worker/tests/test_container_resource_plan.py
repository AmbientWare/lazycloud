from __future__ import annotations

import pytest
from pydantic import ValidationError
from worker.execution import ContainerResourceRequest, plan_oci_linux_resources

MIB = 1024 * 1024


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

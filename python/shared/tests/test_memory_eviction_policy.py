from __future__ import annotations

from shared.container_requests import (
    ContainerMemoryReading,
    select_memory_eviction_candidate,
)

GIB = 1024 * 1024 * 1024


def _reading(container_id: str, current_gib: float, reserved_gib: float) -> ContainerMemoryReading:
    return ContainerMemoryReading(
        container_id=container_id,
        current_bytes=int(current_gib * GIB),
        reserved_bytes=int(reserved_gib * GIB),
    )


def test_the_container_furthest_above_its_request_is_chosen_not_the_largest() -> None:
    """This is the whole reason the decision is not left to the kernel.

    `oom_badness` scores resident size, so it reaches an eight-gibibyte tenant
    sitting exactly inside what it reserved before a one-gibibyte tenant that
    tripled. Reversing that is what makes a reservation worth asking for.
    """
    honest_and_large = _reading("honest", current_gib=8, reserved_gib=8)
    small_and_over = _reading("leaker", current_gib=3, reserved_gib=1)

    chosen = select_memory_eviction_candidate(
        [honest_and_large, small_and_over],
        pressure_percent=50.0,
    )

    assert chosen is not None
    assert chosen.container_id == "leaker"


def test_a_container_inside_its_request_is_never_chosen() -> None:
    """The contract a reservation buys, and the answer to "I cannot be preempted".

    With nothing over its reservation there is no container whose growth caused
    the shortage, so stopping one would be arbitrary.
    """
    within = [
        _reading("a", current_gib=4, reserved_gib=4),
        _reading("b", current_gib=1, reserved_gib=8),
    ]

    assert select_memory_eviction_candidate(within, pressure_percent=90.0) is None


def test_nothing_is_evicted_before_the_machine_is_actually_struggling() -> None:
    """Idle measures around 0.2; a thrashing worker measures around 1.0."""
    over = [_reading("leaker", current_gib=3, reserved_gib=1)]

    assert select_memory_eviction_candidate(over, pressure_percent=0.2) is None
    assert select_memory_eviction_candidate(over, pressure_percent=1.1) is not None

from __future__ import annotations

# The list arm is not an alternative spelling anyone writes. JSON has no tuples,
# so the pair a decorator states in Python reaches a strict model as a list once
# it has crossed the wire, and both have to validate as the same thing.
CpuRequest = int | float | tuple[int | float, int | float] | list[int | float]
"""CPUs to reserve, or a `(reserve, throttle at)` pair. One CPU is one physical core."""

MemoryRequest = str | int | tuple[str | int, str | int] | list[str | int]
"""Memory to reserve, or a `(reserve, kill at)` pair."""

DEFAULT_CPU = 0.125
DEFAULT_FUNCTION_AUTHORIZED = True
DEFAULT_FUNCTION_MEMORY = "128Mi"
DEFAULT_FUNCTION_RETRIES = 3
DEFAULT_FUNCTION_TIMEOUT_SECONDS = 3600
DEFAULT_POD_CPU = 1.0
DEFAULT_HTTP_MEMORY = "128Mi"
DEFAULT_WORKLOAD_PREEMPTIBLE = True


def request_and_limit(
    value: CpuRequest | MemoryRequest | None,
) -> tuple[str | int | float | None, str | int | float | None]:
    """A resource field read as `(request, limit)`, whichever form was written.

    Every reader that only wants the reservation goes through here rather than
    testing the shape itself, so a pair cannot reach arithmetic that assumes a
    scalar and silently multiply a tuple.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError("a resource pair states exactly a request and a limit")
        return value[0], value[1]
    return value, None


__all__ = [
    "DEFAULT_CPU",
    "DEFAULT_FUNCTION_AUTHORIZED",
    "DEFAULT_FUNCTION_MEMORY",
    "DEFAULT_FUNCTION_RETRIES",
    "DEFAULT_FUNCTION_TIMEOUT_SECONDS",
    "DEFAULT_HTTP_MEMORY",
    "DEFAULT_POD_CPU",
    "DEFAULT_WORKLOAD_PREEMPTIBLE",
    "CpuRequest",
    "MemoryRequest",
    "request_and_limit",
]

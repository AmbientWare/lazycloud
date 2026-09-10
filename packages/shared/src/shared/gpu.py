from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from shared.enums import StringEnum

NO_GPU = ""
GPU_ANY = "any"

GpuInput = str | Sequence[str] | None
"""What a caller may write for `gpu=`: one model, an ordered list, or nothing."""


class GpuType(StringEnum):
    NoGPU = NO_GPU
    Any = GPU_ANY
    A10 = "A10"
    A10G = "A10G"
    A100 = "A100"
    A100_40 = "A100-40"
    A100_80 = "A100-80"
    A16 = "A16"
    A30 = "A30"
    A40 = "A40"
    A4000 = "A4000"
    A5000 = "A5000"
    A6000 = "A6000"
    B200 = "B200"
    B300 = "B300"
    GAUDI2 = "GAUDI2"
    GH200 = "GH200"
    H100 = "H100"
    H200 = "H200"
    L4 = "L4"
    L40 = "L40"
    L40S = "L40S"
    RTX3090 = "RTX3090"
    RTX4000Ada = "RTX4000Ada"
    RTX4090 = "RTX4090"
    RTX5090 = "RTX5090"
    RTX6000 = "RTX6000"
    RTX6000Ada = "RTX6000Ada"
    RTXPro6000 = "RTXPro6000"
    T4 = "T4"
    V100 = "V100"
    V100_32 = "V100-32"


SUPPORTED_GPU_TYPES: tuple[GpuType, ...] = (
    GpuType.T4,
    GpuType.A10G,
    GpuType.L4,
    GpuType.L40S,
    GpuType.A100_40,
    GpuType.A100_80,
    GpuType.H100,
    GpuType.H200,
)
"""Models with scheduling and billing contracts, including customer-owned hardware.

Managed offerings are the subset in PLATFORM_GPU_TYPES. Removing a managed
offering must preserve customer hardware and historical billing identities.
"""

SUPPORTED_GPU_NAMES: frozenset[str] = frozenset(gpu.value for gpu in SUPPORTED_GPU_TYPES)

PLATFORM_GPU_TYPES: tuple[GpuType, ...] = (
    GpuType.T4,
    GpuType.A10G,
    GpuType.L4,
    GpuType.L40S,
    GpuType.A100_40,
    GpuType.A100_80,
    GpuType.H100,
    GpuType.H200,
)
"""Products sold on managed capacity; other known models remain valid on customer hardware."""


_GPU_ALIASES: tuple[tuple[str, str], ...] = (
    ("RTXPRO6000", "RTXPro6000"),
    ("RTX6000ADA", "RTX6000Ada"),
    ("RTX4000ADA", "RTX4000Ada"),
    ("RTX6000", "RTX6000"),
    ("RTX5090", "RTX5090"),
    ("RTX4090", "RTX4090"),
    ("RTX3090", "RTX3090"),
    ("V10032G", "V100-32"),
    ("V10032", "V100-32"),
    ("V100", "V100"),
    ("GAUDI2", "GAUDI2"),
    ("GH200", "GH200"),
    ("B300", "B300"),
    ("B200", "B200"),
    ("H200", "H200"),
    ("H100", "H100"),
    ("L40S", "L40S"),
    ("L40", "L40"),
    ("L4", "L4"),
    ("T4", "T4"),
    ("A10080G", "A100-80"),
    ("A10080", "A100-80"),
    ("A10040G", "A100-40"),
    ("A10040", "A100-40"),
    ("A100", "A100"),
    ("A6000", "A6000"),
    ("A5000", "A5000"),
    ("A4000", "A4000"),
    ("A40", "A40"),
    ("A30", "A30"),
    ("A16", "A16"),
    ("A10G", "A10G"),
    ("A10", "A10"),
)


@lru_cache(maxsize=256)
def normalize_gpu_type(value: str) -> str:
    """Resolve a provider or worker GPU label to a canonical model name.

    Cached: billing normalises one label per streamed usage record, and the input
    vocabulary is a handful of names.
    """

    raw = value.strip()
    if raw == "" or raw == "0":
        return NO_GPU

    key = compact_gpu_name(raw)
    for word in ("NVIDIA", "GEFORCE", "TESLA", "QUADRO"):
        key = key.replace(word, "")

    if key == "ANY":
        return GPU_ANY
    if key in {"NOGPU", "NONE", "CPU"}:
        return NO_GPU
    if "A100" in key and "80G" in key:
        return "A100-80"
    if "A100" in key and "40G" in key:
        return "A100-40"

    for alias, gpu_type in _GPU_ALIASES:
        if alias in key:
            return gpu_type
    return raw


def concrete_gpu_type(value: str) -> str:
    """The model a label names, or empty where it names no particular model.

    `any` is a scheduling wildcard and not a card. It reaches worker and pool
    configuration verbatim from what a caller asked for, so anything recording
    which model actually ran has to reject it rather than pass it on as one.
    """

    normalized = normalize_gpu_type(value)
    return NO_GPU if normalized == GPU_ANY else normalized


def compact_gpu_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def gpu_preference(value: GpuInput) -> tuple[str, ...]:
    """The models a request accepts, best first, or empty for no GPU at all.

    Strict where `normalize_gpu_type` is forgiving, and they are not the same
    question. That one reads a label a worker or a provider reports and must
    take whatever it is handed; this one reads what an author asked for, where
    a name the platform cannot schedule has no honest reading. `a100` is the
    one that proves it: it normalises to a real vocabulary member, and no
    worker ever reports it, because a card is a 40GB or an 80GB one. Accepted
    quietly it becomes a workload that places nowhere and says `offer_unavailable`
    at deploy time, hours from the line that caused it.

    Order is the whole point of the sequence form. Earlier entries are
    preferred, and `any` is only meaningful last, since nothing after "whatever
    you have" can ever be reached.
    """

    if value is None:
        return ()
    entries = [value] if isinstance(value, str) else list(value)
    preference: list[str] = []
    for entry in entries:
        normalized = normalize_gpu_type(str(entry))
        if normalized == NO_GPU:
            continue
        if normalized != GPU_ANY and normalized not in SUPPORTED_GPU_NAMES:
            raise ValueError(_unschedulable_gpu_message(entry, normalized))
        if normalized not in preference:
            preference.append(normalized)
    if GPU_ANY in preference and preference[-1] != GPU_ANY:
        unreachable = ", ".join(preference[preference.index(GPU_ANY) + 1 :])
        msg = (
            f"gpu 'any' accepts whatever the platform has, so nothing after it is "
            f"ever reached: {unreachable}. Put it last, or name the models instead."
        )
        raise ValueError(msg)
    return tuple(preference)


def gpu_preference_rank(preference: Sequence[str], candidate: str) -> int | None:
    """Where a card sits in a request's order, or None if it is not accepted.

    The one place `any` is interpreted, so the path that matches an existing
    pool and the path that buys a new one cannot disagree about it. They did:
    one normalised and honoured the wildcard, the other compared raw strings, so
    `gpu="any"` found a pool that existed and could not create the first one.
    """

    if not preference:
        return 0
    normalized = normalize_gpu_type(candidate)
    if normalized == NO_GPU:
        return None
    # Both sides, not just the candidate. `gpu_preference` canonicalises what it
    # returns, but this has to hold for any sequence it is handed: comparing a
    # stored `l4` against a worker's `L4` is the spelling mismatch that let a
    # request provision an instance and then refuse the worker it registered.
    for index, entry in enumerate(preference):
        if entry == GPU_ANY or normalize_gpu_type(entry) == normalized:
            return index
    return None


def gpu_preference_accepts(preference: Sequence[str], candidate: str) -> bool:
    return gpu_preference_rank(preference, candidate) is not None


def _unschedulable_gpu_message(entry: object, normalized: str) -> str:
    # A model with sized variants is the common mistake and the one worth
    # answering directly, rather than making the reader scan the whole list for
    # the name they nearly wrote.
    sized = sorted(name for name in SUPPORTED_GPU_NAMES if name.startswith(f"{normalized}-"))
    if sized:
        return (
            f"gpu {entry!r} does not name a card the platform runs: {normalized} comes "
            f"in more than one size and they are not interchangeable. "
            f"Name one of {', '.join(sized)}."
        )
    supported = ", ".join(sorted(SUPPORTED_GPU_NAMES))
    return f"gpu {entry!r} is not a GPU this platform schedules. Available: {supported}, any."


__all__ = [
    "GPU_ANY",
    "NO_GPU",
    "PLATFORM_GPU_TYPES",
    "SUPPORTED_GPU_NAMES",
    "SUPPORTED_GPU_TYPES",
    "GpuInput",
    "GpuType",
    "compact_gpu_name",
    "concrete_gpu_type",
    "gpu_preference",
    "gpu_preference_accepts",
    "gpu_preference_rank",
    "normalize_gpu_type",
]

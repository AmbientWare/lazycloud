from __future__ import annotations

import re
from functools import lru_cache

from shared.enums import StringEnum

NO_GPU = ""
GPU_ANY = "any"


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
"""The GPU models the platform offers—the single list, not one of two.

`GpuType` is a vocabulary: every name a request, a provider or a worker might
utter, including ones nothing here runs. This is the subset the platform actually
schedules, and it is what both the provider catalogs and the sell-price catalog
are held to. Kept apart, a schedulable list and a priced list drift, and the drift
is only visible as a GPU that runs and cannot be billed, or a rate for hardware
nobody can rent.

Adding a model means adding it here first; the catalogs that must agree then fail
loudly until they do.
"""

SUPPORTED_GPU_NAMES: frozenset[str] = frozenset(gpu.value for gpu in SUPPORTED_GPU_TYPES)


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


__all__ = [
    "GPU_ANY",
    "NO_GPU",
    "SUPPORTED_GPU_NAMES",
    "SUPPORTED_GPU_TYPES",
    "GpuType",
    "compact_gpu_name",
    "concrete_gpu_type",
    "normalize_gpu_type",
]

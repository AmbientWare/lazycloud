from __future__ import annotations


def normalize_gpu_count(requires_gpu: bool, gpu_count: int) -> int:
    if requires_gpu and gpu_count == 0:
        return 1
    return gpu_count

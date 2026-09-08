from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComputeCatalogInstance:
    instance_type: str
    kind: str
    cpu_millicores: int
    memory_mb: int
    gpu: str | None = None
    gpu_count: int = 0


@dataclass(frozen=True, slots=True)
class ComputeCatalogRegion:
    region: str
    instances: tuple[ComputeCatalogInstance, ...]


__all__ = ["ComputeCatalogInstance", "ComputeCatalogRegion"]

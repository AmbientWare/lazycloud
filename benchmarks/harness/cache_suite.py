from __future__ import annotations

from pydantic import Field, field_validator

from benchmarks.harness.models import (
    BenchmarkAccess,
    BenchmarkModel,
    BenchmarkReadPattern,
    ScenarioSpec,
    SuiteSpec,
)
from benchmarks.harness.payload import deterministic_sha256

MIB = 1024 * 1024


class CacheWorkloadSpec(BenchmarkModel):
    access: BenchmarkAccess
    pattern: BenchmarkReadPattern = BenchmarkReadPattern.Sequential
    size_mib: int

    @classmethod
    def parse_entry(cls, entry: str) -> CacheWorkloadSpec:
        parts = [part.strip() for part in entry.split(":")]
        if len(parts) != 3:
            msg = f"invalid cache workload entry {entry!r}; expected access:pattern:size"
            raise ValueError(msg)
        return cls(
            access=BenchmarkAccess(parts[0]),
            pattern=BenchmarkReadPattern(parts[1]),
            size_mib=parse_size_mib(parts[2]),
        )

    @field_validator("size_mib")
    @classmethod
    def _positive_size(cls, value: int) -> int:
        if value <= 0:
            msg = "cache workload size must be positive"
            raise ValueError(msg)
        return value

    @property
    def size_bytes(self) -> int:
        return self.size_mib * MIB

    @property
    def object_path(self) -> str:
        return f"benchmark/{self.access.value}/{self.pattern.value}/{self.size_mib}mib/payload.bin"

    @property
    def label(self) -> str:
        return f"{self.access.value}:{self.pattern.value}:{self.size_mib}mib"

    def expected_sha256(self, nonce: str) -> str:
        return deterministic_sha256(nonce, self.label, self.size_bytes)


class CacheWorkload(BenchmarkModel):
    spec: CacheWorkloadSpec
    object_path: str
    label: str
    size_bytes: int
    expected_sha256: str


class CacheWorkloadPlan(BenchmarkModel):
    suite: str
    nonce: str
    workloads: tuple[CacheWorkload, ...] = Field(default_factory=tuple)

    @property
    def total_bytes(self) -> int:
        return sum(workload.size_bytes for workload in self.workloads)


def parse_size_mib(value: str) -> int:
    text = value.strip().lower()
    multiplier = 1
    for suffix, suffix_multiplier in (
        ("mib", 1),
        ("mb", 1),
        ("gib", 1024),
        ("gb", 1024),
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            multiplier = suffix_multiplier
            break
    size = int(text) * multiplier
    if size <= 0:
        msg = f"cache workload size must be positive: {value!r}"
        raise ValueError(msg)
    return size


def workload_spec_from_scenario(scenario: ScenarioSpec) -> CacheWorkloadSpec | None:
    if scenario.access is None or scenario.size_mib <= 0:
        return None
    return CacheWorkloadSpec(
        access=scenario.access,
        pattern=scenario.pattern,
        size_mib=scenario.size_mib,
    )


def parse_file_plan(plan: str) -> tuple[CacheWorkloadSpec, ...]:
    return tuple(
        CacheWorkloadSpec.parse_entry(part.strip()) for part in plan.split(",") if part.strip()
    )


def plan_cache_workloads(
    suite: SuiteSpec, *, nonce: str = "benchmark-harness"
) -> CacheWorkloadPlan:
    specs = tuple(
        spec
        for scenario in suite.scenarios
        if (spec := workload_spec_from_scenario(scenario)) is not None
    )
    workloads = tuple(_workload_from_spec(spec, nonce) for spec in specs)
    return CacheWorkloadPlan(suite=suite.name, nonce=nonce, workloads=workloads)


def _workload_from_spec(spec: CacheWorkloadSpec, nonce: str) -> CacheWorkload:
    return CacheWorkload(
        spec=spec,
        object_path=spec.object_path,
        label=spec.label,
        size_bytes=spec.size_bytes,
        expected_sha256=spec.expected_sha256(nonce),
    )

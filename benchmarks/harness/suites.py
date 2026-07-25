from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import JsonValue, TypeAdapter

from benchmarks.harness.models import BenchmarkCase, BenchmarkKind, SuiteSpec

_JSON_VALUE_ADAPTER = TypeAdapter[JsonValue](JsonValue)

DEFAULT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        kind=BenchmarkKind.Startup,
        name="python-startup",
        description="Launch a short Python process through the shared process helper.",
    ),
    BenchmarkCase(
        kind=BenchmarkKind.Cache,
        name="storage-service-roundtrip",
        description="Write an object and cache entry through storage services.",
    ),
    BenchmarkCase(
        kind=BenchmarkKind.Sandbox,
        name="sandbox-parallel-plan",
        description="Render the sandbox benchmark plan; live execution uses --sandbox-parallel.",
    ),
)


class SuiteLoader:
    def __init__(self, suite_dir: Path | None = None) -> None:
        self.suite_dir = suite_dir or Path(__file__).resolve().parent / "suite_defs"

    def available(self) -> tuple[str, ...]:
        if not self.suite_dir.exists():
            return ()
        return tuple(sorted(path.stem for path in self.suite_dir.glob("*.yaml")))

    def load(self, name: str) -> SuiteSpec:
        path = self.resolve(name)
        payload = _JSON_VALUE_ADAPTER.validate_python(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        if not isinstance(payload, dict):
            msg = f"suite file {path} must contain a mapping"
            raise ValueError(msg)
        return SuiteSpec.model_validate(payload)

    def resolve(self, name: str) -> Path:
        raw = Path(name)
        candidates: list[Path] = []
        if raw.is_absolute() or raw.parent != Path("."):
            candidates.append(raw)
        else:
            candidates.append(self.suite_dir / raw)
        if candidates[0].suffix not in {".yaml", ".yml"}:
            candidates.extend(Path(f"{path}.yaml") for path in tuple(candidates))
            candidates.extend(Path(f"{path}.yml") for path in tuple(candidates[:1]))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(candidate) for candidate in candidates)
        msg = f"benchmark suite {name!r} not found; searched {searched}"
        raise FileNotFoundError(msg)


def case_for(kind: BenchmarkKind) -> BenchmarkCase:
    for item in DEFAULT_CASES:
        if item.kind == kind:
            return item
    msg = f"unsupported benchmark case: {kind}"
    raise ValueError(msg)


def merge_suite_args(*mappings: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    merged: dict[str, JsonValue] = {}
    for mapping in mappings:
        merged.update({key: value for key, value in mapping.items() if value is not None})
    return merged


def available_suites() -> tuple[str, ...]:
    return SuiteLoader().available()

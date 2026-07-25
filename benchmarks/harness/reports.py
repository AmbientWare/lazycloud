from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable

from benchmarks.harness.models import (
    BenchmarkMeasurement,
    BenchmarkReport,
    BenchmarkValidationFailure,
)


def report_to_json(report: BenchmarkReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def report_to_markdown(report: BenchmarkReport) -> str:
    lines = [
        "# Benchmark Report",
        "",
        "| case | status | duration_ms |",
        "| --- | --- | ---: |",
    ]
    for result in report.results:
        lines.append(f"| {result.case.name} | {result.status.value} | {result.duration_ms:.2f} |")
    lines.append("")
    return "\n".join(lines)


def measurements_to_jsonl(measurements: Iterable[BenchmarkMeasurement]) -> str:
    lines = [json.dumps(item.model_dump(mode="json"), sort_keys=True) for item in measurements]
    return "\n".join(lines) + ("\n" if lines else "")


def measurements_to_markdown(
    measurements: Iterable[BenchmarkMeasurement],
    *,
    failures: Iterable[BenchmarkValidationFailure] = (),
) -> str:
    measurement_list = list(measurements)
    failure_list = list(failures)
    lines = [
        "# Benchmark Measurements",
        "",
        f"- Status: `{'failed' if failure_list else 'ok'}`",
        f"- Measurements: `{len(measurement_list)}`",
        "",
        "| Suite | Scenario | Measurement | Size | MB/s | Status | Evidence |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for measurement in measurement_list:
        size = measurement.tags.get("size_mib") or (
            round(measurement.bytes_read / 1024 / 1024, 2) if measurement.bytes_read else ""
        )
        lines.append(
            "| "
            f"{measurement.suite} | {measurement.scenario} | {measurement.measurement.value} | "
            f"{size} | {measurement.mbps:.2f} | `{measurement.status.value}` | "
            f"{_evidence_bits(measurement)} |"
        )
    lines.extend(["", "## Failures", ""])
    if failure_list:
        lines.extend(
            f"- `{failure.kind.value}` {failure.suite}/{failure.scenario}/"
            f"{failure.measurement.value}: {failure.message}"
            for failure in failure_list
        )
    else:
        lines.append("- none")
    lines.extend(["", *measurement_rollup_markdown(measurement_list)])
    return "\n".join(lines) + "\n"


def measurement_rollup_markdown(measurements: Iterable[BenchmarkMeasurement]) -> list[str]:
    grouped: dict[tuple[str, str], list[BenchmarkMeasurement]] = defaultdict(list)
    for measurement in measurements:
        grouped[(measurement.suite, measurement.measurement.value)].append(measurement)
    if not grouped:
        return []
    lines = [
        "## Rollup",
        "",
        "| Suite | Measurement | Count | Best MB/s | Worst MB/s |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for (suite, measurement_name), values in sorted(grouped.items()):
        mbps_values = [item.mbps for item in values if item.mbps > 0]
        best = max(mbps_values) if mbps_values else 0
        worst = min(mbps_values) if mbps_values else 0
        lines.append(f"| {suite} | {measurement_name} | {len(values)} | {best:.2f} | {worst:.2f} |")
    lines.append("")
    return lines


def _evidence_bits(measurement: BenchmarkMeasurement) -> str:
    evidence = measurement.evidence
    bits: list[str] = []
    for key, value in (
        ("sha_ok", evidence.sha_ok),
        ("cache_hit", evidence.cache_hit),
        ("remote_worker", evidence.remote_worker),
        ("remote_node", evidence.remote_node),
        ("cloud_read", evidence.cloud_read),
        ("cache_source", evidence.cache_source),
        ("artifact_output", evidence.artifact_output),
    ):
        if value is not None:
            bits.append(f"{key}={value}")
    return "`" + " ".join(bits) + "`" if bits else ""

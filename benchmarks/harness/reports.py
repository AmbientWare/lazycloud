from __future__ import annotations

import json

from benchmarks.harness.models import (
    BenchmarkReport,
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

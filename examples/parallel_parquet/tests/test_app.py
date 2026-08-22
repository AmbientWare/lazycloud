from __future__ import annotations

import json
from pathlib import Path

import examples.parallel_parquet.app as example
import pytest


@pytest.mark.parametrize(
    "key",
    [
        "/absolute/path.parquet",
        "../escape.parquet",
        "safe/../escape.parquet",
        "safe\\escape.parquet",
        "safe//partition.parquet",
        "safe/./partition.parquet",
        " safe/partition.parquet",
        "safe/partition.parquet ",
        "safe/",
    ],
)
def test_object_keys_reject_ambiguous_or_escaping_paths(key: str) -> None:
    with pytest.raises(ValueError):
        example.validate_object_key(key, field="test key")


def _partition_payload(key: str, start: int) -> example.PartitionResult:
    return {
        "key": key,
        "row_count": 2,
        "amount_cents": 500,
        "min_record_id": start,
        "max_record_id": start + 1,
        "category_counts": {"even": 1, "odd": 1},
    }


def test_write_summary_aggregates_validated_results_and_persists_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(example, "BUCKET_ROOT", tmp_path)
    partitions = [
        _partition_payload(f"{example.CONFIG.input_prefix}/part-000.parquet", 0),
        _partition_payload(f"{example.CONFIG.input_prefix}/part-001.parquet", 2),
    ]

    summary = example.write_summary.local(partitions, ["task-a", "task-b"])

    assert summary["partition_count"] == 2
    assert summary["row_count"] == 4
    assert summary["amount_cents"] == 1000
    output = json.loads((tmp_path / example.CONFIG.output_key).read_text(encoding="utf-8"))
    assert output == summary


def test_write_summary_rejects_empty_results_and_ambiguous_task_lineage() -> None:
    partition = _partition_payload(
        f"{example.CONFIG.input_prefix}/part-000.parquet",
        0,
    )

    with pytest.raises(ValueError, match="at least one"):
        example.write_summary.local([], [])
    with pytest.raises(ValueError, match="non-empty and unique"):
        example.write_summary.local([partition, partition], ["task-a", "task-a"])

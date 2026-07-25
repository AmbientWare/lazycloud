from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import examples.parallel_parquet.app as example
import pytest
from shared.tasks import TaskStatus


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


@dataclass(frozen=True)
class _FakeHandle:
    task_id: str


@dataclass(frozen=True)
class _FakeResult:
    id: str
    status: TaskStatus
    ok: bool
    value: Any = None
    error: str = ""


class _FakeBatch:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = results
        self.handles = tuple(_FakeHandle(result.id) for result in results)

    def __iter__(self):
        return iter(self.handles)

    def wait(
        self,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> list[_FakeResult]:
        assert timeout_seconds == 1800
        assert poll_interval_seconds == 1
        return self.results


class _FakeQueueTarget:
    def __init__(self, batch: _FakeBatch, observed: list[list[str]]) -> None:
        self.batch = batch
        self.observed = observed

    def put_many(self, items: list[str]) -> _FakeBatch:
        self.observed.append(items)
        return self.batch


def _partition_payload(key: str, start: int) -> example.PartitionResult:
    return {
        "key": key,
        "row_count": 2,
        "amount_cents": 500,
        "min_record_id": start,
        "max_record_id": start + 1,
        "category_counts": {"even": 1, "odd": 1},
    }


def test_run_batch_fans_out_real_task_handles_and_writes_only_complete_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [
        f"{example.CONFIG.input_prefix}/part-000.parquet",
        f"{example.CONFIG.input_prefix}/part-001.parquet",
    ]
    batch = _FakeBatch(
        [
            _FakeResult("task-a", TaskStatus.Complete, True, _partition_payload(keys[0], 0)),
            _FakeResult("task-b", TaskStatus.Complete, True, _partition_payload(keys[1], 2)),
        ]
    )
    enqueued: list[list[str]] = []
    written: list[tuple[list[example.PartitionResult], list[str]]] = []

    def fake_seed_remote(
        partition_count: int,
        rows_per_partition: int,
        overwrite: bool,
    ) -> list[str]:
        assert (partition_count, rows_per_partition, overwrite) == (2, 2, False)
        return keys

    def fail_list_remote() -> list[str]:
        raise AssertionError("list should not run")

    def fake_queue_target(target: str) -> _FakeQueueTarget:
        assert target == "deployed"
        return _FakeQueueTarget(batch, enqueued)

    monkeypatch.setattr(example.seed_partitions, "remote", fake_seed_remote)
    monkeypatch.setattr(example.list_partitions, "remote", fail_list_remote)
    monkeypatch.setattr(example.process_partition, "target", fake_queue_target)

    def fake_write(
        partitions: list[example.PartitionResult],
        task_ids: list[str],
    ) -> example.BatchSummary:
        written.append((partitions, task_ids))
        return {
            "input_prefix": example.CONFIG.input_prefix,
            "output_key": example.CONFIG.output_key,
            "partition_count": 2,
            "row_count": 4,
            "amount_cents": 1000,
            "task_ids": task_ids,
            "partitions": partitions,
        }

    monkeypatch.setattr(example.write_summary, "remote", fake_write)

    summary = example.run_batch(True, 2, 2, False)

    assert enqueued == [keys]
    assert written[0][1] == ["task-a", "task-b"]
    assert summary["row_count"] == 4
    assert summary["amount_cents"] == 1000


def test_run_batch_fails_before_summary_when_any_partition_task_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [
        f"{example.CONFIG.input_prefix}/part-000.parquet",
        f"{example.CONFIG.input_prefix}/part-001.parquet",
    ]
    batch = _FakeBatch(
        [
            _FakeResult("task-a", TaskStatus.Complete, True, _partition_payload(keys[0], 0)),
            _FakeResult(
                "task-b",
                TaskStatus.Failed,
                False,
                error="invalid Parquet footer",
            ),
        ]
    )
    monkeypatch.setattr(example.list_partitions, "remote", lambda: keys)

    def fake_queue_target(target: str) -> _FakeQueueTarget:
        assert target == "deployed"
        return _FakeQueueTarget(batch, [])

    def fail_write_summary(
        partitions: list[example.PartitionResult],
        task_ids: list[str],
    ) -> example.BatchSummary:
        del partitions, task_ids
        raise AssertionError("summary must not be written")

    monkeypatch.setattr(example.process_partition, "target", fake_queue_target)
    monkeypatch.setattr(example.write_summary, "remote", fail_write_summary)

    with pytest.raises(RuntimeError, match=r"task-b.*invalid Parquet footer"):
        example.run_batch(False)


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

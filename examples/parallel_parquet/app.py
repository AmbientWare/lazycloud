from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self, TypedDict
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from lazycloud import App, CloudBucket, CloudBucketConfig, Image

APP_NAME = "parallel_parquet"
BUCKET_ROOT = Path("/data")
PYARROW_VERSION = "25.0.0"
PYARROW_REQUIREMENT = f"pyarrow=={PYARROW_VERSION}"
DEFAULT_BUCKET = "lazycloud-examples"
DEFAULT_REGION = "us-east-1"
DEFAULT_INPUT_PREFIX = "examples/parallel-parquet/input"
DEFAULT_OUTPUT_KEY = "examples/parallel-parquet/output/summary.json"

BUCKET_ENV = "LAZYCLOUD_PARQUET_BUCKET"
REGION_ENV = "LAZYCLOUD_PARQUET_REGION"
INPUT_PREFIX_ENV = "LAZYCLOUD_PARQUET_INPUT_PREFIX"
OUTPUT_KEY_ENV = "LAZYCLOUD_PARQUET_OUTPUT_KEY"
ENDPOINT_ENV = "LAZYCLOUD_PARQUET_ENDPOINT"
ACCESS_KEY_SECRET_ENV = "LAZYCLOUD_PARQUET_ACCESS_KEY_SECRET"
SECRET_KEY_SECRET_ENV = "LAZYCLOUD_PARQUET_SECRET_KEY_SECRET"

_CONFIG_ENV_NAMES = (
    BUCKET_ENV,
    REGION_ENV,
    INPUT_PREFIX_ENV,
    OUTPUT_KEY_ENV,
    ENDPOINT_ENV,
    ACCESS_KEY_SECRET_ENV,
    SECRET_KEY_SECRET_ENV,
)
_SECRET_NAME_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")


class PartitionResult(TypedDict):
    key: str
    row_count: int
    amount_cents: int
    min_record_id: int
    max_record_id: int
    category_counts: dict[str, int]


class BatchSummary(TypedDict):
    input_prefix: str
    output_key: str
    partition_count: int
    row_count: int
    amount_cents: int
    task_ids: list[str]
    partitions: list[PartitionResult]


class _PartitionResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str
    row_count: int = Field(ge=1)
    amount_cents: int = Field(ge=0)
    min_record_id: int
    max_record_id: int
    category_counts: dict[str, int]

    @model_validator(mode="after")
    def result_is_consistent(self) -> Self:
        if self.min_record_id > self.max_record_id:
            raise ValueError("minimum record id must not exceed maximum record id")
        if any(not category or count < 0 for category, count in self.category_counts.items()):
            raise ValueError("category counts require non-empty names and non-negative counts")
        if sum(self.category_counts.values()) != self.row_count:
            raise ValueError("category counts must equal the partition row count")
        return self


_PARTITION_RESULTS = TypeAdapter(list[_PartitionResultModel])


@dataclass(frozen=True, slots=True)
class ParquetExampleConfig:
    bucket: str
    region: str
    input_prefix: str
    output_key: str
    endpoint: str = ""
    access_key_secret: str = ""
    secret_key_secret: str = ""

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> ParquetExampleConfig:
        values = os.environ if source is None else source
        bucket = _plain_value(values.get(BUCKET_ENV, DEFAULT_BUCKET), field="bucket")
        region = _plain_value(values.get(REGION_ENV, DEFAULT_REGION), field="region")
        input_prefix = validate_object_key(
            values.get(INPUT_PREFIX_ENV, DEFAULT_INPUT_PREFIX),
            field="input prefix",
        )
        output_key = validate_object_key(
            values.get(OUTPUT_KEY_ENV, DEFAULT_OUTPUT_KEY),
            field="output key",
            suffix=".json",
        )
        if output_key.startswith(f"{input_prefix}/"):
            raise ValueError("output key must not be inside the input prefix")

        endpoint = validate_endpoint(values.get(ENDPOINT_ENV, ""))
        access_key_secret = validate_secret_name(
            values.get(ACCESS_KEY_SECRET_ENV, ""),
            field="access-key secret",
        )
        secret_key_secret = validate_secret_name(
            values.get(SECRET_KEY_SECRET_ENV, ""),
            field="secret-key secret",
        )
        if bool(access_key_secret) != bool(secret_key_secret):
            raise ValueError(
                "access-key and secret-key secret names must both be set or both be omitted"
            )
        return cls(
            bucket=bucket,
            region=region,
            input_prefix=input_prefix,
            output_key=output_key,
            endpoint=endpoint,
            access_key_secret=access_key_secret,
            secret_key_secret=secret_key_secret,
        )

    def workload_env(self) -> dict[str, str]:
        values = {
            BUCKET_ENV: self.bucket,
            REGION_ENV: self.region,
            INPUT_PREFIX_ENV: self.input_prefix,
            OUTPUT_KEY_ENV: self.output_key,
        }
        if self.endpoint:
            values[ENDPOINT_ENV] = self.endpoint
        if self.access_key_secret:
            values[ACCESS_KEY_SECRET_ENV] = self.access_key_secret
            values[SECRET_KEY_SECRET_ENV] = self.secret_key_secret
        return values


def validate_object_key(value: str, *, field: str, suffix: str = "") -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must use safe POSIX path characters")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError(f"{field} must be a canonical relative object key")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must be a canonical relative object key")
    if path.as_posix() != value:
        raise ValueError(f"{field} must be a canonical relative object key")
    if suffix and not value.endswith(suffix):
        raise ValueError(f"{field} must end with {suffix}")
    return value


def validate_partition_key(input_prefix: str, key: str) -> str:
    prefix = validate_object_key(input_prefix, field="input prefix")
    validated = validate_object_key(key, field="partition key", suffix=".parquet")
    if not validated.startswith(f"{prefix}/"):
        raise ValueError("partition key must be inside the configured input prefix")
    return validated


def validate_endpoint(value: str) -> str:
    if not value:
        return ""
    if value != value.strip():
        raise ValueError("endpoint must not contain surrounding whitespace")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be an absolute HTTP(S) URL without credentials or query")
    return value.rstrip("/")


def validate_secret_name(value: str, *, field: str) -> str:
    if not value:
        return ""
    if value != value.strip() or _SECRET_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be an uppercase environment-style name")
    return value


def _plain_value(value: str, *, field: str) -> str:
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be one non-empty path-safe value")
    return value


def _positive_count(value: int, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or value < 1 or value > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return value


def object_path(key: str) -> Path:
    return BUCKET_ROOT / validate_object_key(key, field="object key")


CONFIG = ParquetExampleConfig.from_env()
WORKLOAD_ENV = CONFIG.workload_env()
data_bucket = CloudBucket(
    CONFIG.bucket,
    str(BUCKET_ROOT),
    CloudBucketConfig(
        bucket=CONFIG.bucket,
        region=CONFIG.region,
        endpoint=CONFIG.endpoint or None,
        force_path_style=bool(CONFIG.endpoint),
        access_key=CONFIG.access_key_secret or None,
        secret_key=CONFIG.secret_key_secret or None,
    ),
)
parquet_image = Image(
    python_version="3.12",
    python_packages=[PYARROW_REQUIREMENT],
)
app = App(APP_NAME)


_SEED_SCRIPT = """
import shutil
import sys
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

root = Path(sys.argv[1])
prefix = sys.argv[2]
partition_count = int(sys.argv[3])
rows_per_partition = int(sys.argv[4])
target_root = root / prefix
target_root.mkdir(parents=True, exist_ok=True)
staging = Path(tempfile.mkdtemp(prefix=".seed-", dir=target_root))
try:
    for partition in range(partition_count):
        start = partition * rows_per_partition
        record_ids = list(range(start, start + rows_per_partition))
        table = pa.table(
            {
                "record_id": record_ids,
                "amount_cents": [(record_id + 1) * 125 for record_id in record_ids],
                "category": ["even" if record_id % 2 == 0 else "odd" for record_id in record_ids],
            }
        )
        pq.write_table(
            table,
            staging / f"part-{partition:03d}.parquet",
            compression="snappy",
        )
    for staged in sorted(staging.glob("*.parquet")):
        staged.replace(target_root / staged.name)
finally:
    shutil.rmtree(staging, ignore_errors=True)
"""

_PROCESS_SCRIPT = """
import collections
import json
import sys

import pyarrow.parquet as pq

path = sys.argv[1]
key = sys.argv[2]
table = pq.read_table(path, columns=["record_id", "amount_cents", "category"])
record_ids = table.column("record_id").to_pylist()
amounts = table.column("amount_cents").to_pylist()
categories = table.column("category").to_pylist()
if not record_ids:
    raise ValueError("partition is empty")
if any(not isinstance(value, int) or isinstance(value, bool) for value in record_ids):
    raise TypeError("record_id must contain integers")
if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in amounts):
    raise TypeError("amount_cents must contain non-negative integers")
if any(not isinstance(value, str) or not value for value in categories):
    raise TypeError("category must contain non-empty strings")
print(
    json.dumps(
        {
            "key": key,
            "row_count": table.num_rows,
            "amount_cents": sum(amounts),
            "min_record_id": min(record_ids),
            "max_record_id": max(record_ids),
            "category_counts": dict(sorted(collections.Counter(categories).items())),
        },
        separators=(",", ":"),
    )
)
"""


def _run_pyarrow(script: str, *args: str, timeout_seconds: float = 900) -> str:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"PyArrow workload timed out after {timeout_seconds:g} seconds") from exc
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        detail = lines[-1] if lines else f"exit code {completed.returncode}"
        raise RuntimeError(f"PyArrow workload failed: {detail}")
    return completed.stdout.strip()


@app.function(
    name="seed-partitions",
    image=parquet_image,
    cpu=1.0,
    memory="1Gi",
    timeout_seconds=900,
    retries=0,
    env=WORKLOAD_ENV,
    volumes=[data_bucket],
)
def seed_partitions(
    partition_count: int = 4,
    rows_per_partition: int = 10,
    overwrite: bool = False,
) -> list[str]:
    partition_count = _positive_count(partition_count, field="partition count", maximum=32)
    rows_per_partition = _positive_count(
        rows_per_partition,
        field="rows per partition",
        maximum=10_000,
    )
    keys = [
        f"{CONFIG.input_prefix}/part-{partition:03d}.parquet"
        for partition in range(partition_count)
    ]
    paths = [object_path(key) for key in keys]
    existing = [key for key, path in zip(keys, paths, strict=True) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "seed partitions already exist; pass overwrite=true only for disposable example data: "
            + ", ".join(existing)
        )
    _run_pyarrow(
        _SEED_SCRIPT,
        str(BUCKET_ROOT),
        CONFIG.input_prefix,
        str(partition_count),
        str(rows_per_partition),
    )
    missing = [key for key, path in zip(keys, paths, strict=True) if not path.is_file()]
    if missing:
        raise RuntimeError("seed did not create every partition: " + ", ".join(missing))
    return keys


@app.function(
    name="list-partitions",
    image=parquet_image,
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=120,
    retries=0,
    env=WORKLOAD_ENV,
    volumes=[data_bucket],
)
def list_partitions() -> list[str]:
    input_root = object_path(CONFIG.input_prefix)
    if not input_root.is_dir():
        raise FileNotFoundError(f"input prefix does not exist: {CONFIG.input_prefix}")
    keys = [
        validate_partition_key(CONFIG.input_prefix, path.relative_to(BUCKET_ROOT).as_posix())
        for path in sorted(input_root.rglob("*.parquet"))
        if path.is_file()
    ]
    if not keys:
        raise FileNotFoundError(f"input prefix has no Parquet partitions: {CONFIG.input_prefix}")
    return keys


@app.task_queue(
    name="process-partition",
    image=parquet_image,
    cpu=1.0,
    memory="1Gi",
    timeout=900,
    retries=1,
    retry_for=[RuntimeError],
    retry_delay_seconds=2,
    workers=4,
    keep_warm_seconds=0,
    max_pending_tasks=100,
    env=WORKLOAD_ENV,
    volumes=[data_bucket],
)
def process_partition(key: str) -> PartitionResult:
    validated_key = validate_partition_key(CONFIG.input_prefix, key)
    path = object_path(validated_key)
    if not path.is_file():
        raise FileNotFoundError(f"partition does not exist: {validated_key}")
    output = _run_pyarrow(_PROCESS_SCRIPT, str(path), validated_key)
    try:
        payload = _PartitionResultModel.model_validate_json(output)
    except ValidationError as exc:
        raise RuntimeError("partition processor returned an invalid result") from exc
    if payload.key != validated_key:
        raise RuntimeError("partition processor returned a mismatched key")
    return _partition_result(payload)


@app.function(
    name="write-summary",
    image=parquet_image,
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=120,
    retries=0,
    env=WORKLOAD_ENV,
    volumes=[data_bucket],
)
def write_summary(partitions: list[PartitionResult], task_ids: list[str]) -> BatchSummary:
    validated = _PARTITION_RESULTS.validate_python(partitions)
    if not validated:
        raise ValueError("at least one partition result is required")
    if len(validated) != len(task_ids):
        raise ValueError("one task id is required for every partition result")
    if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("task ids must be non-empty and unique")
    keys = [validate_partition_key(CONFIG.input_prefix, item.key) for item in validated]
    if len(set(keys)) != len(keys):
        raise ValueError("partition results must not contain duplicate keys")
    partition_values = [_partition_result(item) for item in validated]
    summary: BatchSummary = {
        "input_prefix": CONFIG.input_prefix,
        "output_key": CONFIG.output_key,
        "partition_count": len(partition_values),
        "row_count": sum(item["row_count"] for item in partition_values),
        "amount_cents": sum(item["amount_cents"] for item in partition_values),
        "task_ids": list(task_ids),
        "partitions": partition_values,
    }
    target = object_path(CONFIG.output_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _partition_result(value: _PartitionResultModel) -> PartitionResult:
    return {
        "key": value.key,
        "row_count": value.row_count,
        "amount_cents": value.amount_cents,
        "min_record_id": value.min_record_id,
        "max_record_id": value.max_record_id,
        "category_counts": dict(value.category_counts),
    }


def run_batch(
    seed: bool = False,
    partition_count: int = 4,
    rows_per_partition: int = 10,
    overwrite_seed: bool = False,
) -> BatchSummary:
    if seed:
        partition_keys = seed_partitions.remote(
            partition_count,
            rows_per_partition,
            overwrite_seed,
        )
    else:
        partition_keys = list_partitions.remote()
    if not partition_keys:
        raise RuntimeError("no Parquet partitions were selected")
    validated_keys = [validate_partition_key(CONFIG.input_prefix, key) for key in partition_keys]

    batch = process_partition.target("deployed").put_many(validated_keys)
    task_ids = [handle.task_id for handle in batch]
    if len(task_ids) != len(validated_keys):
        raise RuntimeError("partition batch returned an incomplete Task handle set")
    results = batch.wait(timeout_seconds=1800, poll_interval_seconds=1)
    failures = [
        f"{result.id} ({result.status.value}): {result.error or 'no error detail'}"
        for result in results
        if not result.ok
    ]
    if failures:
        raise RuntimeError(
            "partition batch failed; summary was not written: " + "; ".join(failures)
        )
    if len(results) != len(validated_keys):
        raise RuntimeError("partition batch returned an incomplete result set")

    partitions: list[PartitionResult] = []
    for expected_key, result in zip(validated_keys, results, strict=True):
        try:
            payload = _PartitionResultModel.model_validate(result.value)
        except ValidationError as exc:
            raise RuntimeError(f"partition task {result.id} returned an invalid result") from exc
        if payload.key != expected_key:
            raise RuntimeError(f"partition task {result.id} returned a mismatched key")
        partitions.append(_partition_result(payload))
    return write_summary.remote(partitions, task_ids)


__all__ = [
    "APP_NAME",
    "CONFIG",
    "BatchSummary",
    "ParquetExampleConfig",
    "PartitionResult",
    "app",
    "data_bucket",
    "list_partitions",
    "object_path",
    "parquet_image",
    "process_partition",
    "run_batch",
    "seed_partitions",
    "validate_endpoint",
    "validate_object_key",
    "validate_partition_key",
    "write_summary",
]

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from database.tables.observability import (
    UsageBillingContributionTable,
    UsageBillingWindowTable,
    UsageRecordTable,
)
from pydantic import BaseModel, JsonValue, TypeAdapter
from shared.gpu import normalize_gpu_type
from shared.usage import UsageBillingOwner, UsageMetric, UsageRecord
from sqlalchemy import (
    DateTime,
    Integer,
    String,
    case,
    cast,
    delete,
    extract,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

type UsageMetadataScalar = str | int | float | bool | None
type BillingWindowValue = str | int | float | datetime


class UsageBillingContributionBase(TypedDict):
    workspace_id: str
    app_id: str
    workload_id: str
    billing_at: datetime
    resource_id: str
    worker_id: str
    window_start_ms: int
    window_end_ms: int
    legacy_record_id: str
    billing_owner: str
    gpu_type: str


class UsageBillingAggregateResult(BaseModel):
    app_id: str
    workload_id: str
    billing_owner: str
    gpu_type: str
    priced_on: date
    bucket_index: int
    cpu_seconds: float | None
    memory_gib_seconds: float | None
    gpu_seconds: float | None
    runs: int | None
    omitted_duration_records: int | None


class UsageBillingContributionTotals(BaseModel):
    contribution_count: int
    billing_at: datetime | None
    billing_owner: str | None
    gpu_type: str | None
    cpu_direct_records: int | None
    cpu_direct_seconds: float | None
    cpu_derived_seconds: float | None
    memory_direct_records: int | None
    memory_direct_gib_seconds: float | None
    memory_derived_gib_seconds: float | None
    gpu_direct_records: int | None
    gpu_direct_seconds: float | None
    gpu_derived_seconds: float | None
    runs: int | None
    omitted_duration_records: int | None


@dataclass(frozen=True, slots=True)
class UsageBillingEvidenceRow:
    id: str
    resource_type: str
    resource_id: str
    metric: UsageMetric
    quantity: float
    created_at: datetime
    app_id: str
    workload_id: str
    billing_owner: str
    cpu_millicores: str
    memory_mb: str
    gpu_count: str
    gpu: str
    """GPU model the container held, empty for CPU-only work.

    Carried into billing because GPU seconds are priced per model: a T4 second and
    an H100 second differ by more than an order of magnitude, so a rate that did
    not name the model could only be a blend nobody could check.
    """
    label_worker_id: str
    metadata_worker_id: str
    window_start_ms: UsageMetadataScalar
    window_end_ms: UsageMetadataScalar
    metering_window_started_at: str


@dataclass(frozen=True, slots=True)
class UsageBillingAggregateRow:
    app_id: str
    workload_id: str
    billing_owner: str
    gpu_type: str
    priced_on: date
    """The day this usage ran, which decides the rate it prices at.

    Distinct from `bucket_start`, which is a property of how the report was asked
    for: buckets begin where the requested window begins, and a single-bucket
    report has no day in it at all.
    """

    bucket_start: datetime
    cpu_seconds: float
    memory_gib_seconds: float
    gpu_seconds: float
    runs: int
    omitted_duration_records: int


@dataclass(frozen=True, slots=True)
class UsageBillingWindowContribution:
    workspace_id: str
    app_id: str
    workload_id: str
    billing_at: datetime
    resource_id: str
    worker_id: str
    window_start_ms: int
    window_end_ms: int
    legacy_record_id: str
    billing_owner: str = ""
    gpu_type: str = ""
    cpu_direct_records: int = 0
    cpu_direct_seconds: float = 0
    cpu_derived_seconds: float = 0
    memory_direct_records: int = 0
    memory_direct_gib_seconds: float = 0
    memory_derived_gib_seconds: float = 0
    gpu_direct_records: int = 0
    gpu_direct_seconds: float = 0
    gpu_derived_seconds: float = 0
    runs: int = 0
    omitted_duration_records: int = 0

    @property
    def identity(self) -> UsageBillingWindowIdentity:
        return UsageBillingWindowIdentity(
            workspace_id=self.workspace_id,
            app_id=self.app_id,
            workload_id=self.workload_id,
            resource_id=self.resource_id,
            worker_id=self.worker_id,
            window_start_ms=self.window_start_ms,
            window_end_ms=self.window_end_ms,
            legacy_record_id=self.legacy_record_id,
        )

    @property
    def has_value(self) -> bool:
        return any(
            value != 0
            for value in (
                self.cpu_direct_records,
                self.cpu_direct_seconds,
                self.cpu_derived_seconds,
                self.memory_direct_records,
                self.memory_direct_gib_seconds,
                self.memory_derived_gib_seconds,
                self.gpu_direct_records,
                self.gpu_direct_seconds,
                self.gpu_derived_seconds,
                self.runs,
                self.omitted_duration_records,
            )
        )

    def values(self, *, record_id: str) -> dict[str, BillingWindowValue]:
        return {
            "record_id": record_id,
            "workspace_id": self.workspace_id,
            "app_id": self.app_id,
            "workload_id": self.workload_id,
            "billing_at": self.billing_at,
            "resource_id": self.resource_id,
            "worker_id": self.worker_id,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "legacy_record_id": self.legacy_record_id,
            "billing_owner": self.billing_owner,
            "gpu_type": self.gpu_type,
            "cpu_direct_records": self.cpu_direct_records,
            "cpu_direct_seconds": self.cpu_direct_seconds,
            "cpu_derived_seconds": self.cpu_derived_seconds,
            "memory_direct_records": self.memory_direct_records,
            "memory_direct_gib_seconds": self.memory_direct_gib_seconds,
            "memory_derived_gib_seconds": self.memory_derived_gib_seconds,
            "gpu_direct_records": self.gpu_direct_records,
            "gpu_direct_seconds": self.gpu_direct_seconds,
            "gpu_derived_seconds": self.gpu_derived_seconds,
            "runs": self.runs,
            "omitted_duration_records": self.omitted_duration_records,
        }


@dataclass(frozen=True, slots=True, order=True)
class UsageBillingWindowIdentity:
    workspace_id: str
    app_id: str
    workload_id: str
    resource_id: str
    worker_id: str
    window_start_ms: int
    window_end_ms: int
    legacy_record_id: str


_BILLING_USAGE_METRICS = (
    UsageMetric.CpuSeconds,
    UsageMetric.MemoryGibSeconds,
    UsageMetric.GpuSeconds,
    UsageMetric.TaskCount,
    UsageMetric.ContainerDurationMilliseconds,
)
_CONTAINER_COMPUTE_METRICS = frozenset(
    {
        UsageMetric.ContainerDurationMilliseconds,
        UsageMetric.CpuSeconds,
        UsageMetric.MemoryGibSeconds,
        UsageMetric.GpuSeconds,
    }
)
_IDENTITY_COLUMNS = (
    UsageBillingWindowTable.workspace_id,
    UsageBillingWindowTable.app_id,
    UsageBillingWindowTable.workload_id,
    UsageBillingWindowTable.resource_id,
    UsageBillingWindowTable.worker_id,
    UsageBillingWindowTable.window_start_ms,
    UsageBillingWindowTable.window_end_ms,
    UsageBillingWindowTable.legacy_record_id,
)
_BILLING_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:[.][0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@dataclass(slots=True)
class UsageBillingRepository:
    session: Session

    def lock_record(self, *, workspace_id: str, record_id: str) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:usage_record_lock_key, 0))"),
            {"usage_record_lock_key": f"{workspace_id}:{record_id}"},
        )

    def evidence(
        self,
        *,
        workspace_id: str,
        start: datetime,
        end: datetime,
        app_id: str | None = None,
    ) -> Iterator[UsageBillingEvidenceRow]:
        metering_started_at = _usage_json_text(
            self.session,
            "metadata",
            "metering_window_started_at",
        )
        if self.session.get_bind().dialect.name == "postgresql":
            billing_at = case(
                (
                    metering_started_at.regexp_match(_BILLING_TIMESTAMP_PATTERN),
                    cast(metering_started_at, DateTime(timezone=True)),
                ),
                else_=UsageRecordTable.created_at,
            )
            billing_window = (billing_at >= start, billing_at < end)
        else:
            valid_timezone = or_(
                metering_started_at.like("%Z"),
                metering_started_at.like("%+__:__"),
                metering_started_at.like("%-__:__"),
            )
            billing_epoch = func.coalesce(
                case((valid_timezone, func.julianday(metering_started_at))),
                func.julianday(UsageRecordTable.created_at),
            )
            billing_window = (
                billing_epoch >= func.julianday(start),
                billing_epoch < func.julianday(end),
            )
        statement = select(UsageRecordTable).where(
            UsageRecordTable.workspace_id == workspace_id,
            *billing_window,
            UsageRecordTable.metric.in_(tuple(metric.value for metric in _BILLING_USAGE_METRICS)),
        )
        if app_id is not None:
            statement = statement.where(
                func.coalesce(_usage_json_text(self.session, "labels", "app_id"), "") == app_id
            )
        statement = statement.execution_options(yield_per=2_000)
        for row in self.session.scalars(statement):
            record = UsageRecord.model_validate(row.payload)
            metadata = _JSON_OBJECT_ADAPTER.validate_python(record.metadata)
            yield UsageBillingEvidenceRow(
                id=record.id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                metric=record.metric,
                quantity=record.quantity,
                created_at=record.created_at.astimezone(UTC),
                app_id=record.labels.get("app_id", ""),
                workload_id=record.labels.get("stub_id", ""),
                billing_owner=record.labels.get("billing_owner", ""),
                cpu_millicores=record.labels.get("cpu_millicores", ""),
                memory_mb=record.labels.get("mem_mb", ""),
                gpu_count=record.labels.get("gpu_count", ""),
                gpu=normalize_gpu_type(record.labels.get("gpu", "")),
                label_worker_id=record.labels.get("worker_id", ""),
                metadata_worker_id=_metadata_text(metadata.get("worker_id")),
                window_start_ms=_metadata_scalar(metadata.get("window_start_ms")),
                window_end_ms=_metadata_scalar(metadata.get("window_end_ms")),
                metering_window_started_at=_metadata_text(
                    metadata.get("metering_window_started_at")
                ),
            )

    def apply_record_change(
        self,
        *,
        current: UsageRecord,
    ) -> None:
        stored = self.session.get(UsageBillingContributionTable, current.id)
        desired = usage_billing_window_contribution(current)
        if desired is not None and not desired.has_value:
            desired = None
        identities = {
            identity
            for identity in (
                _stored_contribution_identity(stored) if stored is not None else None,
                desired.identity if desired is not None else None,
            )
            if identity is not None
        }
        self._lock_windows(identities)
        if stored is not None:
            self.session.delete(stored)
        if desired is not None:
            self.session.add(UsageBillingContributionTable(**desired.values(record_id=current.id)))
        self.session.flush()
        for identity in sorted(identities):
            self._recompute_window(identity)
        self.session.flush()

    def workspaces_with_usage_between(self, *, start: datetime, end: datetime) -> tuple[str, ...]:
        """Every workspace with metered compute in an interval.

        The daily pricing job sweeps these rather than every workspace, so a run
        costs one query plus one report per workspace that actually ran
        something — not one per workspace that has ever existed.
        """

        rows = self.session.scalars(
            select(UsageBillingWindowTable.workspace_id)
            .where(
                UsageBillingWindowTable.billing_at >= start,
                UsageBillingWindowTable.billing_at < end,
            )
            .group_by(UsageBillingWindowTable.workspace_id)
            .order_by(UsageBillingWindowTable.workspace_id.asc())
        ).all()
        return tuple(rows)

    def aggregates(
        self,
        *,
        workspace_id: str,
        start: datetime,
        end: datetime,
        bucket_seconds: int | None,
        group_by_workload: bool,
        app_id: str | None = None,
    ) -> tuple[UsageBillingAggregateRow, ...]:
        dialect = self.session.get_bind().dialect.name
        billing_epoch = (
            extract("epoch", UsageBillingWindowTable.billing_at)
            if dialect == "postgresql"
            else (func.julianday(UsageBillingWindowTable.billing_at) - 2_440_587.5) * 86_400
        )
        bucket = (
            cast(
                func.floor((billing_epoch - start.timestamp()) / bucket_seconds),
                Integer,
            )
            if bucket_seconds is not None
            else literal(0)
        )
        bucket = case((bucket < 0, 0), else_=bucket).label("bucket_index")
        # The day the usage happened, which is not derivable from the bucket: a
        # report's buckets start wherever its window starts, and one asking for a
        # single bucket has no day in it at all. A rate is chosen by this.
        priced_on = func.date(UsageBillingWindowTable.billing_at).label("priced_on")
        workload = (
            UsageBillingWindowTable.workload_id if group_by_workload else literal("")
        ).label("workload_id")
        cpu = case(
            (
                UsageBillingWindowTable.cpu_direct_records > 0,
                UsageBillingWindowTable.cpu_direct_seconds,
            ),
            else_=UsageBillingWindowTable.cpu_derived_seconds,
        )
        memory = case(
            (
                UsageBillingWindowTable.memory_direct_records > 0,
                UsageBillingWindowTable.memory_direct_gib_seconds,
            ),
            else_=UsageBillingWindowTable.memory_derived_gib_seconds,
        )
        gpu = case(
            (
                UsageBillingWindowTable.gpu_direct_records > 0,
                UsageBillingWindowTable.gpu_direct_seconds,
            ),
            else_=UsageBillingWindowTable.gpu_derived_seconds,
        )
        statement = (
            select(
                UsageBillingWindowTable.app_id,
                workload,
                bucket,
                priced_on,
                UsageBillingWindowTable.billing_owner,
                UsageBillingWindowTable.gpu_type,
                func.sum(cpu).label("cpu_seconds"),
                func.sum(memory).label("memory_gib_seconds"),
                func.sum(gpu).label("gpu_seconds"),
                func.sum(UsageBillingWindowTable.runs).label("runs"),
                func.sum(UsageBillingWindowTable.omitted_duration_records).label(
                    "omitted_duration_records"
                ),
            )
            .where(
                UsageBillingWindowTable.workspace_id == workspace_id,
                UsageBillingWindowTable.billing_at >= start,
                UsageBillingWindowTable.billing_at < end,
            )
            .group_by(
                UsageBillingWindowTable.app_id,
                workload,
                bucket,
                priced_on,
                UsageBillingWindowTable.billing_owner,
                UsageBillingWindowTable.gpu_type,
            )
        )
        if app_id is not None:
            statement = statement.where(UsageBillingWindowTable.app_id == app_id)
        return tuple(
            UsageBillingAggregateRow(
                app_id=row.app_id,
                workload_id=row.workload_id,
                billing_owner=row.billing_owner,
                gpu_type=row.gpu_type,
                priced_on=_as_date(row.priced_on),
                bucket_start=start
                + timedelta(seconds=int(row.bucket_index) * (bucket_seconds or 0)),
                cpu_seconds=float(row.cpu_seconds or 0),
                memory_gib_seconds=float(row.memory_gib_seconds or 0),
                gpu_seconds=float(row.gpu_seconds or 0),
                runs=int(row.runs or 0),
                omitted_duration_records=int(row.omitted_duration_records or 0),
            )
            for row in (
                UsageBillingAggregateResult.model_validate(raw)
                for raw in self.session.execute(statement).mappings()
            )
        )

    def _lock_windows(self, identities: set[UsageBillingWindowIdentity]) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        for identity in sorted(identities):
            lock_key = json.dumps(
                (
                    identity.workspace_id,
                    identity.app_id,
                    identity.workload_id,
                    identity.resource_id,
                    identity.worker_id,
                    identity.window_start_ms,
                    identity.window_end_ms,
                    identity.legacy_record_id,
                ),
                separators=(",", ":"),
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:window_lock_key, 0))"),
                {"window_lock_key": lock_key},
            )

    def _recompute_window(self, identity: UsageBillingWindowIdentity) -> None:
        raw_totals = (
            self.session.execute(
                select(
                    func.count().label("contribution_count"),
                    func.min(UsageBillingContributionTable.billing_at).label("billing_at"),
                    # Every contribution to a window shares its GPU model and
                    # its billing owner: one container, one worker, one unit.
                    # `max` rather than `min` so a stated classification beats an
                    # unstated one — the empty string sorts below every name.
                    func.max(UsageBillingContributionTable.billing_owner).label("billing_owner"),
                    func.max(UsageBillingContributionTable.gpu_type).label("gpu_type"),
                    func.sum(UsageBillingContributionTable.cpu_direct_records).label(
                        "cpu_direct_records"
                    ),
                    func.sum(UsageBillingContributionTable.cpu_direct_seconds).label(
                        "cpu_direct_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.cpu_derived_seconds).label(
                        "cpu_derived_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.memory_direct_records).label(
                        "memory_direct_records"
                    ),
                    func.sum(UsageBillingContributionTable.memory_direct_gib_seconds).label(
                        "memory_direct_gib_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.memory_derived_gib_seconds).label(
                        "memory_derived_gib_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.gpu_direct_records).label(
                        "gpu_direct_records"
                    ),
                    func.sum(UsageBillingContributionTable.gpu_direct_seconds).label(
                        "gpu_direct_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.gpu_derived_seconds).label(
                        "gpu_derived_seconds"
                    ),
                    func.sum(UsageBillingContributionTable.runs).label("runs"),
                    func.sum(UsageBillingContributionTable.omitted_duration_records).label(
                        "omitted_duration_records"
                    ),
                ).where(*_contribution_identity_predicates(identity))
            )
            .mappings()
            .one()
        )
        row = UsageBillingContributionTotals.model_validate(raw_totals)
        if int(row.contribution_count) == 0:
            self.session.execute(
                delete(UsageBillingWindowTable).where(*_identity_predicates(identity))
            )
            return
        if not isinstance(row.billing_at, datetime):
            raise TypeError("usage billing contribution requires a billing timestamp")
        values: dict[str, BillingWindowValue] = {
            "workspace_id": identity.workspace_id,
            "app_id": identity.app_id,
            "workload_id": identity.workload_id,
            "billing_at": row.billing_at,
            "resource_id": identity.resource_id,
            "worker_id": identity.worker_id,
            "window_start_ms": identity.window_start_ms,
            "window_end_ms": identity.window_end_ms,
            "legacy_record_id": identity.legacy_record_id,
            "billing_owner": row.billing_owner or "",
            "gpu_type": row.gpu_type or "",
            "cpu_direct_records": int(row.cpu_direct_records or 0),
            "cpu_direct_seconds": float(row.cpu_direct_seconds or 0),
            "cpu_derived_seconds": float(row.cpu_derived_seconds or 0),
            "memory_direct_records": int(row.memory_direct_records or 0),
            "memory_direct_gib_seconds": float(row.memory_direct_gib_seconds or 0),
            "memory_derived_gib_seconds": float(row.memory_derived_gib_seconds or 0),
            "gpu_direct_records": int(row.gpu_direct_records or 0),
            "gpu_direct_seconds": float(row.gpu_direct_seconds or 0),
            "gpu_derived_seconds": float(row.gpu_derived_seconds or 0),
            "runs": int(row.runs or 0),
            "omitted_duration_records": int(row.omitted_duration_records or 0),
        }
        dialect = self.session.get_bind().dialect.name
        insert_statement = (
            postgresql_insert(UsageBillingWindowTable).values(**values)
            if dialect == "postgresql"
            else sqlite_insert(UsageBillingWindowTable).values(**values)
        )
        excluded = insert_statement.excluded
        self.session.execute(
            insert_statement.on_conflict_do_update(
                index_elements=list(_IDENTITY_COLUMNS),
                set_={
                    "billing_at": excluded.billing_at,
                    "billing_owner": excluded.billing_owner,
                    "gpu_type": excluded.gpu_type,
                    "cpu_direct_records": excluded.cpu_direct_records,
                    "cpu_direct_seconds": excluded.cpu_direct_seconds,
                    "cpu_derived_seconds": excluded.cpu_derived_seconds,
                    "memory_direct_records": excluded.memory_direct_records,
                    "memory_direct_gib_seconds": excluded.memory_direct_gib_seconds,
                    "memory_derived_gib_seconds": excluded.memory_derived_gib_seconds,
                    "gpu_direct_records": excluded.gpu_direct_records,
                    "gpu_direct_seconds": excluded.gpu_direct_seconds,
                    "gpu_derived_seconds": excluded.gpu_derived_seconds,
                    "runs": excluded.runs,
                    "omitted_duration_records": excluded.omitted_duration_records,
                    "updated_at": func.now(),
                },
            )
        )


def usage_billing_window_contribution(
    record: UsageRecord,
) -> UsageBillingWindowContribution | None:
    if record.metric not in _BILLING_USAGE_METRICS:
        return None
    if (
        record.resource_type == "container"
        and record.labels.get("billing_owner") == UsageBillingOwner.SelfHosted.value
        and record.metric in _CONTAINER_COMPUTE_METRICS
    ):
        return None
    quantity = max(record.quantity, 0)
    app_id = record.labels.get("app_id", "")
    workload_id = record.labels.get("stub_id", "")
    if not workload_id and record.metric is UsageMetric.TaskCount:
        workload_id = record.resource_id
    window_start_ms = _metadata_int(record.metadata.get("window_start_ms"))
    window_end_ms = _metadata_int(record.metadata.get("window_end_ms"))
    valid_window = (
        window_start_ms is not None
        and window_end_ms is not None
        and window_start_ms >= 0
        and window_end_ms > window_start_ms
    )
    worker_id = _metadata_text(record.metadata.get("worker_id")) or record.labels.get(
        "worker_id", ""
    )
    if valid_window:
        assert window_start_ms is not None
        assert window_end_ms is not None
        contribution_worker_id = worker_id
        contribution_window_start_ms = window_start_ms
        contribution_window_end_ms = window_end_ms
        legacy_record_id = ""
    else:
        contribution_worker_id = ""
        contribution_window_start_ms = -1
        contribution_window_end_ms = -1
        legacy_record_id = record.id
    base: UsageBillingContributionBase = {
        "workspace_id": record.workspace_id,
        "app_id": app_id,
        "workload_id": workload_id,
        "billing_at": _billing_moment(record),
        "resource_id": record.resource_id,
        "worker_id": contribution_worker_id,
        "window_start_ms": contribution_window_start_ms,
        "window_end_ms": contribution_window_end_ms,
        "legacy_record_id": legacy_record_id,
        "billing_owner": record.labels.get("billing_owner", ""),
        "gpu_type": normalize_gpu_type(record.labels.get("gpu", "")),
    }
    if record.metric is UsageMetric.ContainerDurationMilliseconds:
        seconds = quantity / 1_000
        cpu = _nonnegative_number(record.labels.get("cpu_millicores", ""))
        memory = _nonnegative_number(record.labels.get("mem_mb", ""))
        gpu = _nonnegative_number(record.labels.get("gpu_count", ""))
        return UsageBillingWindowContribution(
            **base,
            cpu_derived_seconds=seconds * cpu / 1_000,
            memory_derived_gib_seconds=seconds * memory / 1_024,
            gpu_derived_seconds=seconds * gpu,
            omitted_duration_records=int(not any(value > 0 for value in (cpu, memory, gpu))),
        )
    if record.metric is UsageMetric.CpuSeconds:
        return UsageBillingWindowContribution(
            **base,
            cpu_direct_records=1,
            cpu_direct_seconds=quantity,
        )
    if record.metric is UsageMetric.MemoryGibSeconds:
        return UsageBillingWindowContribution(
            **base,
            memory_direct_records=1,
            memory_direct_gib_seconds=quantity,
        )
    if record.metric is UsageMetric.GpuSeconds:
        return UsageBillingWindowContribution(
            **base,
            gpu_direct_records=1,
            gpu_direct_seconds=quantity,
        )
    if record.metric is UsageMetric.TaskCount:
        return UsageBillingWindowContribution(**base, runs=round(quantity))
    # No catch-all: an unrecognised metric contributes nothing.
    return None


def _stored_contribution_identity(
    contribution: UsageBillingContributionTable,
) -> UsageBillingWindowIdentity:
    return UsageBillingWindowIdentity(
        workspace_id=str(contribution.workspace_id),
        app_id=contribution.app_id,
        workload_id=contribution.workload_id,
        resource_id=contribution.resource_id,
        worker_id=contribution.worker_id,
        window_start_ms=contribution.window_start_ms,
        window_end_ms=contribution.window_end_ms,
        legacy_record_id=contribution.legacy_record_id,
    )


def _identity_predicates(identity: UsageBillingWindowIdentity):
    return (
        UsageBillingWindowTable.workspace_id == identity.workspace_id,
        UsageBillingWindowTable.app_id == identity.app_id,
        UsageBillingWindowTable.workload_id == identity.workload_id,
        UsageBillingWindowTable.resource_id == identity.resource_id,
        UsageBillingWindowTable.worker_id == identity.worker_id,
        UsageBillingWindowTable.window_start_ms == identity.window_start_ms,
        UsageBillingWindowTable.window_end_ms == identity.window_end_ms,
        UsageBillingWindowTable.legacy_record_id == identity.legacy_record_id,
    )


def _contribution_identity_predicates(identity: UsageBillingWindowIdentity):
    return (
        UsageBillingContributionTable.workspace_id == identity.workspace_id,
        UsageBillingContributionTable.app_id == identity.app_id,
        UsageBillingContributionTable.workload_id == identity.workload_id,
        UsageBillingContributionTable.resource_id == identity.resource_id,
        UsageBillingContributionTable.worker_id == identity.worker_id,
        UsageBillingContributionTable.window_start_ms == identity.window_start_ms,
        UsageBillingContributionTable.window_end_ms == identity.window_end_ms,
        UsageBillingContributionTable.legacy_record_id == identity.legacy_record_id,
    )


def _billing_moment(record: UsageRecord) -> datetime:
    value = record.metadata.get("metering_window_started_at")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return record.created_at.astimezone(UTC)
        if moment.tzinfo is not None and moment.utcoffset() is not None:
            return moment.astimezone(UTC)
    return record.created_at.astimezone(UTC)


def _metadata_int(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _metadata_text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _as_date(value: object) -> date:
    """Backends differ on whether `date()` returns a date or a string."""

    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _nonnegative_number(value: str) -> float:
    try:
        return max(float(value or "0"), 0)
    except ValueError:
        return 0


def _metadata_scalar(value: JsonValue) -> UsageMetadataScalar:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"usage metadata scalar has unsupported type: {type(value).__name__}")


def _usage_json_text(session: Session, *path: str) -> ColumnElement[str]:
    if session.get_bind().dialect.name == "postgresql":
        return func.jsonb_extract_path_text(UsageRecordTable.payload, *path, type_=String)
    json_path = "$." + ".".join(path)
    return func.json_extract(UsageRecordTable.payload, json_path, type_=String)


__all__ = [
    "UsageBillingAggregateRow",
    "UsageBillingEvidenceRow",
    "UsageBillingRepository",
    "UsageBillingWindowContribution",
    "UsageMetadataScalar",
    "usage_billing_window_contribution",
]

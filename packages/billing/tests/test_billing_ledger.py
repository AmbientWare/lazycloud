from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.billing_ledger import BillingLedgerRepository
from pydantic import JsonValue
from shared.billing import BillableMetric
from shared.timestamps import utc_now
from shared.usage import UsageMetric, UsageRecord, UsageUnit

from billing import BillingLedgerService, utc_day_bounds


def test_repricing_a_day_replaces_it_rather_than_adding_to_it(
    isolated_services: ApiServices,
) -> None:
    """A day is priced more than once, and the last run is what is owed.

    Usage arrives late and runs are retried, so the second pass sees a different
    figure from the first. Appending bills both. Inserting only what is new keeps
    the stale figure and ignores the correction. Either way the invoice is wrong,
    and only a run whose numbers actually change can tell the three apart.

    GPU seconds force `variant` into the identity: nine models share one metric
    and differ by an order of magnitude, so a key without it folds them together
    and bills eight at the ninth's rate.
    """

    day = utc_now().date()
    start, end = utc_day_bounds(day)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    def meter(gpu: str, seconds: float) -> None:
        metadata: dict[str, JsonValue] = {
            "worker_id": "worker-ledger",
            "window_start_ms": 0,
            "window_end_ms": 1000,
            "gpu": gpu,
        }
        isolated_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=f"container-{gpu}-{seconds}",
                metric=UsageMetric.GpuSeconds,
                quantity=seconds,
                unit=UsageUnit.Seconds,
                labels={"gpu": gpu, "gpu_count": "1", "worker_id": "worker-ledger"},
                metadata=metadata,
                created_at=start + timedelta(hours=1),
            )
        )

    def price_the_day() -> None:
        overview = isolated_services.usage.billing_overview(
            workspace_id=workspace_id, start=start, end=end, bucket_seconds=86_400
        )
        with isolated_services.context.database.session() as session:
            BillingLedgerService(session).record_day(overview)
            session.commit()

    meter("H100", 4.0)
    meter("T4", 9.0)
    price_the_day()
    # Late usage for one model only: the run that follows must move H100 and
    # leave T4 exactly where it was.
    meter("H100", 6.0)
    price_the_day()

    with isolated_services.context.database.session() as session:
        entries = BillingLedgerRepository(session).for_day(workspace_id, day)

    gpu_lines = {e.variant: e for e in entries if e.metric is BillableMetric.GpuSeconds}
    assert set(gpu_lines) == {"H100", "T4"}
    assert gpu_lines["H100"].quantity == 10.0
    assert gpu_lines["T4"].quantity == 9.0
    assert gpu_lines["H100"].cost_nanos > gpu_lines["T4"].cost_nanos


def test_a_ledger_day_refuses_a_report_that_is_not_that_day(
    isolated_services: ApiServices,
) -> None:
    """The interval is read off the report, so a month cannot be filed as a day.

    `billing_overview` prices whatever window it is asked for, and a month is a
    perfectly ordinary answer. Writing one under a single date would charge a
    month's compute to one day, permanently and without an error.
    """

    day = utc_now().date()
    start, _ = utc_day_bounds(day)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    month = isolated_services.usage.billing_overview(
        workspace_id=workspace_id,
        start=start,
        end=start + timedelta(days=30),
        bucket_seconds=86_400,
    )

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ValueError, match="one whole UTC day"),
    ):
        BillingLedgerService(session).record_day(month)


def test_a_line_that_moves_variants_does_not_leave_its_old_row_behind(
    isolated_services: ApiServices,
) -> None:
    """A corrected observation moves a line; the row it came from has to go.

    Usage is deduplicated on an id built from the metric, workspace, container,
    worker and window — not from the GPU model — so re-sending the same
    observation with a corrected model moves its seconds from one variant to
    another. The abandoned key is simply absent from the recomputation. Left in
    place it stays priced, indistinguishable from a real line, and is invoiced on
    top of the corrected one.
    """

    day = utc_now().date()
    start, end = utc_day_bounds(day)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    record_id = str(uuid4())

    def meter_as(gpu: str) -> None:
        metadata: dict[str, JsonValue] = {
            "worker_id": "worker-relabel",
            "window_start_ms": 0,
            "window_end_ms": 1000,
            "gpu": gpu,
        }
        isolated_services.usage.append(
            UsageRecord(
                id=record_id,
                workspace_id=workspace_id,
                resource_type="container",
                resource_id="container-relabel",
                metric=UsageMetric.GpuSeconds,
                quantity=8.0,
                unit=UsageUnit.Seconds,
                labels={"gpu": gpu, "gpu_count": "1", "worker_id": "worker-relabel"},
                metadata=metadata,
                created_at=start + timedelta(hours=2),
            )
        )

    def price_the_day() -> None:
        overview = isolated_services.usage.billing_overview(
            workspace_id=workspace_id, start=start, end=end, bucket_seconds=86_400
        )
        with isolated_services.context.database.session() as session:
            BillingLedgerService(session).record_day(overview)
            session.commit()

    meter_as("A100-40")
    price_the_day()
    meter_as("H100")
    price_the_day()

    with isolated_services.context.database.session() as session:
        entries = BillingLedgerRepository(session).for_day(workspace_id, day)

    gpu_variants = {e.variant for e in entries if e.metric is BillableMetric.GpuSeconds}
    assert gpu_variants == {"H100"}

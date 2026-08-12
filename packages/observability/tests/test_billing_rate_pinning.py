from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from api.server.services import ApiServices
from observability.billing import DEFAULT_SELL_PRICES, SellPrice, UsagePriceCatalog
from pydantic import JsonValue
from shared.billing import BillableMetric
from shared.usage import UsageMetric, UsageRecord, UsageUnit


def _catalog_with_cpu_rise(rise_on: date) -> UsagePriceCatalog:
    others = tuple(
        price for price in DEFAULT_SELL_PRICES if price.metric is not BillableMetric.CpuSeconds
    )
    before = SellPrice(
        metric=BillableMetric.CpuSeconds,
        label="CPU",
        unit=UsageUnit.Seconds,
        price_per_unit_nanos=10_000,
        currency="USD",
        effective_date=date(2020, 1, 1),
    )
    after = SellPrice(
        metric=BillableMetric.CpuSeconds,
        label="CPU",
        unit=UsageUnit.Seconds,
        price_per_unit_nanos=20_000,
        currency="USD",
        effective_date=rise_on,
    )
    return UsagePriceCatalog(currency="USD", prices=(*others, before, after))


def test_usage_prices_at_the_rate_in_force_when_it_ran(
    isolated_services: ApiServices,
) -> None:
    """A rate change moves what tomorrow costs, never what yesterday did.

    A report is rebuilt whenever it is asked for, so without this a price rise
    silently reprices every month still open — and a customer who has already
    been told what they owe is told something else. The two rates land on their
    own lines rather than blending into an average nobody was charged.
    """

    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    rise_on = now.date()
    yesterday = now - timedelta(days=1)
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    for moment, marker in ((yesterday, "before"), (now, "after")):
        metadata: dict[str, JsonValue] = {
            "worker_id": f"worker-{marker}",
            "window_start_ms": 0,
            "window_end_ms": 1000,
        }
        isolated_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="container",
                resource_id=f"container-{marker}",
                metric=UsageMetric.CpuSeconds,
                quantity=100.0,
                unit=UsageUnit.Seconds,
                labels={"cpu_millicores": "1000", "worker_id": f"worker-{marker}"},
                metadata=metadata,
                created_at=moment,
            )
        )

    dated = replace(isolated_services.usage, price_catalog=_catalog_with_cpu_rise(rise_on))
    window_start = yesterday - timedelta(hours=1)
    window_end = now + timedelta(hours=1)

    # Every path that prices, not just the one that reads evidence rows. The
    # overview and the workload breakdown read the rollup, and both derived their
    # day from the report's own bucketing — which starts wherever the caller
    # asked, so a bucket straddling a rate change priced all of it at one rate.
    summaries = {
        "report": dated.billing_report(
            workspace_id=workspace_id,
            start=window_start,
            end=window_end,
            bucket_seconds=86_400,
        ).summary,
        "overview": dated.billing_overview(
            workspace_id=workspace_id,
            start=window_start,
            end=window_end,
            bucket_seconds=86_400,
        ).summary,
    }
    workloads = dated.billing_workloads(
        workspace_id=workspace_id,
        app_id="",
        start=window_start,
        end=window_end,
        bucket_seconds=86_400,
    )

    for name, summary in summaries.items():
        cpu = {
            line.effective_date: line
            for line in summary
            if line.metric is BillableMetric.CpuSeconds
        }
        assert set(cpu) == {date(2020, 1, 1), rise_on}, name
        assert cpu[date(2020, 1, 1)].price_per_unit_nanos == 10_000, name
        assert cpu[rise_on].price_per_unit_nanos == 20_000, name
        # Yesterday's hundred seconds keep the old rate; today's take the new one.
        assert cpu[date(2020, 1, 1)].cost_nanos == 1_000_000, name
        assert cpu[rise_on].cost_nanos == 2_000_000, name

    workload_rates = {
        line.effective_date
        for item in workloads.data
        for line in item.lines
        if line.metric is BillableMetric.CpuSeconds
    }
    assert workload_rates == {date(2020, 1, 1), rise_on}

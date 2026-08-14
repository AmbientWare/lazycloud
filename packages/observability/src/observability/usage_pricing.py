from __future__ import annotations

from dataclasses import dataclass

from database.repositories.billing_ledger import BillingLedgerRepository, FrozenSpan
from database.repositories.execution import EventRepository
from database.types import DatabaseSession
from pydantic import JsonValue
from shared.billing_quotes import UnpricedSpan
from shared.events import EventLevel
from shared.usage import UsageRecord

UNPRICED_SPAN_ACTION = "billing.span.unpriced"
REPRICE_REFUSED_ACTION = "billing.span.reprice_refused"


@dataclass(frozen=True, slots=True)
class MeteredUsagePricer:
    """Turns one metered record into the cost it freezes.

    Built on the caller's session so the cost lands in the transaction that wrote
    the record it prices. A crash between the two would either lose money or
    count it twice, and neither is recoverable from the usage row alone.
    """

    session: DatabaseSession

    def price(self, record: UsageRecord) -> None:
        """Freeze what a metered record costs, and report what could not be.

        A metric that is attribution or telemetry produces no charge and nothing
        is written. A window published rates covered writes its segments and its
        allowance increment, including at a rate of an explicit zero — that is a
        segment stating the dimension was metered and free, which a window no
        rate covered never gets. A gap no rate covered writes nothing, invents no
        zero, and leaves a durable error naming what to publish. A record the
        ledger had already priced keeps the cost it froze, and a recomputation
        that disagrees with it leaves a durable error naming both figures.
        """

        pricing = BillingLedgerRepository(self.session).price_record(record)
        if isinstance(pricing, UnpricedSpan):
            self._report_unpriced(record, pricing)
        elif isinstance(pricing, FrozenSpan) and pricing.disagrees:
            self._report_reprice_refused(record, pricing)

    def _report_unpriced(self, record: UsageRecord, span: UnpricedSpan) -> None:
        """Record the money that was measured and not charged.

        Cluster-scoped: a span nothing priced is a defect in this platform's rate
        card, and the workspace feed belongs to the customer whose work it failed
        to price. The gap is stated as instants so an operator can publish a rate
        covering it and re-price the interval.
        """

        data: dict[str, JsonValue] = {
            "workspace_id": record.workspace_id,
            "usage_record_id": record.id,
            "metric": record.metric.value,
            "dimension": span.dimension.value,
            "reason": span.reason.value,
            "gap_started_at": span.gap_started_at.isoformat(),
            "gap_ended_at": span.gap_ended_at.isoformat(),
        }
        EventRepository(self.session).records.create_across_workspaces(
            {
                "action": UNPRICED_SPAN_ACTION,
                "level": EventLevel.Error.value,
                "resource_type": record.resource_type,
                "resource_id": record.resource_id,
                "message": (
                    f"{span.dimension.value} usage was metered but not priced: {span.reason.value}"
                ),
                "data": data,
            }
        )

    def _report_reprice_refused(self, record: UsageRecord, span: FrozenSpan) -> None:
        """Record that a record was re-sent under a figure the ledger will not take.

        The frozen cost stands, because a customer has already been shown it and
        the provider has already been metered from it. What arrived instead is
        stated beside it so an operator can find the producer that changed a
        quantity under an id it had already used; correcting the charge is a new
        record, never an edit to this one.
        """

        data: dict[str, JsonValue] = {
            "workspace_id": record.workspace_id,
            "usage_record_id": record.id,
            "metric": record.metric.value,
            "dimension": span.dimension.value,
            "frozen_cost_nanos": span.cost_nanos,
            "recomputed_cost_nanos": span.recomputed_cost_nanos,
        }
        EventRepository(self.session).records.create_across_workspaces(
            {
                "action": REPRICE_REFUSED_ACTION,
                "level": EventLevel.Error.value,
                "resource_type": record.resource_type,
                "resource_id": record.resource_id,
                "message": (
                    f"{record.id} was re-recorded and now prices at "
                    f"{span.recomputed_cost_nanos} nanodollars; the ledger keeps the "
                    f"{span.cost_nanos} it froze"
                ),
                "data": data,
            }
        )


__all__ = ["REPRICE_REFUSED_ACTION", "UNPRICED_SPAN_ACTION", "MeteredUsagePricer"]

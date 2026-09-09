from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from database.repositories.economics import EconomicsLedgerFacts, EconomicsRepository
from pydantic import AwareDatetime, Field, model_validator
from shared.billing_credits import CreditKind
from shared.billing_quotes import LedgerBasis, LedgerComponent
from shared.contracts import ContractModel
from shared.errors import InvalidInputError
from shared.usage import UsageBillingOwner
from sqlalchemy import text

from billing.costs import MAX_COST_WINDOW_DAYS
from database import DatabaseClient

Nanos = Annotated[int, Field(ge=0)]
Seconds = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class EconomicsPeriod(ContractModel):
    scope: Literal["installation"] = "installation"
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_period(self) -> EconomicsPeriod:
        if (
            not timedelta(0)
            < self.ended_at - self.started_at
            <= timedelta(days=MAX_COST_WINDOW_DAYS)
        ):
            raise ValueError(f"economics periods must span at most {MAX_COST_WINDOW_DAYS} days")
        return self


class StatementComponent(StrEnum):
    GrossUsageControl = "gross_usage_control"
    SubscriptionRevenue = "recognized_subscription_revenue"
    RefundsAndWriteoffs = "revenue_refunds_and_writeoffs"
    SupplierCompute = "supplier_compute_including_disk_and_ipv4"
    ObjectStorage = "object_storage_capacity"
    ObjectOperations = "object_storage_operations"
    Network = "network_including_nat_and_cross_zone"
    PaymentFees = "payment_processing_fees"
    FixedInfrastructure = "fixed_infrastructure"
    PrepaidCashReceived = "prepaid_cash_received"
    PrepaidCashRefunded = "prepaid_cash_refunded"
    PrepaidOpeningLiability = "prepaid_opening_liability"
    PrepaidClosingLiability = "prepaid_closing_liability"


class StatementEntry(ContractModel):
    component: StatementComponent
    amount_nanos: Nanos
    reference: str = Field(min_length=1)
    basis: Literal["actual_statement"] = "actual_statement"


class EconomicsStatement(EconomicsPeriod):
    """Consolidated, nonoverlapping totals for this installation and exact period.

    A reference names a reconciled statement line, never a supplier catalog quote.
    Gross usage excludes tax. Refunds/writeoffs exclude locally recorded waivers
    and credit allocations. Fixed infrastructure excludes workload supplier costs.
    """

    attribution_basis: Literal["segment_started_at"]
    entries: list[StatementEntry]

    @model_validator(mode="after")
    def distinct_evidence(self) -> EconomicsStatement:
        components = [entry.component for entry in self.entries]
        references = [entry.reference for entry in self.entries]
        if len(components) != len(set(components)) or len(references) != len(set(references)):
            raise ValueError("each statement component and source line may appear only once")
        return self


class ResourceSeconds(ContractModel):
    cpu_core_seconds: Seconds
    memory_gib_seconds: Seconds
    gpu_card_seconds: Seconds


class OperationalStatement(EconomicsPeriod):
    """Historical platform-fleet observations, separate from supplier invoices.

    Capacity uses the same allocatable CPU/memory/GPU units as the reservation
    ledger. Stranded capacity is a measured subset of unallocated capacity;
    interruption recovery overlaps purchased time and is never added to its cost.
    """

    reference: str = Field(min_length=1)
    capacity: ResourceSeconds | None = None
    reserved_capacity: ResourceSeconds | None = None
    stranded_capacity: ResourceSeconds | None = None
    provisioned_node_seconds: Seconds | None = None
    idle_node_seconds: Seconds | None = None
    interruption_count: Annotated[int, Field(ge=0)] | None = None
    interruption_recovery_seconds: Seconds | None = None

    @model_validator(mode="after")
    def bounded_observations(self) -> OperationalStatement:
        if (
            self.idle_node_seconds is not None
            and self.provisioned_node_seconds is not None
            and self.idle_node_seconds > self.provisioned_node_seconds
        ):
            raise ValueError("idle node time cannot exceed provisioned node time")
        if (
            self.capacity is not None
            and self.stranded_capacity is not None
            and any(
                stranded > total
                for stranded, total in zip(
                    _resource_values(self.stranded_capacity),
                    _resource_values(self.capacity),
                    strict=True,
                )
            )
        ):
            raise ValueError("stranded capacity cannot exceed observed capacity")
        if self.capacity is not None and self.reserved_capacity is not None:
            for index, (reserved, capacity) in enumerate(
                zip(
                    _resource_values(self.reserved_capacity),
                    _resource_values(self.capacity),
                    strict=True,
                )
            ):
                stranded = (
                    _resource_values(self.stranded_capacity)[index]
                    if self.stranded_capacity is not None
                    else Decimal(0)
                )
                if reserved + stranded > capacity:
                    raise ValueError("reserved and stranded capacity exceed observed capacity")
        return self


class EconomicsStatus(StrEnum):
    Incomplete = "incomplete"
    ContributionLoss = "contribution_loss"
    OperatingLoss = "operating_loss"
    BreakEven = "break_even"
    Profitable = "profitable"


class UsageRevenue(ContractModel):
    billing_owner: str
    component: LedgerComponent
    basis: LedgerBasis
    quantity: Decimal
    gross_nanos: Nanos


class OccupancyReport(ContractModel):
    billed_reservations: ResourceSeconds
    observations: OperationalStatement | None
    cpu_reservation_ratio: Decimal | None
    memory_reservation_ratio: Decimal | None
    gpu_reservation_ratio: Decimal | None
    gaps: list[str]


class EconomicsReport(EconomicsPeriod):
    attribution_basis: Literal["segment_started_at"] = "segment_started_at"
    observed_at: AwareDatetime
    status: EconomicsStatus
    usage: list[UsageRevenue]
    gross_usage_nanos: Nanos
    trial_credit_applied_nanos: Nanos
    subscription_credit_applied_nanos: Nanos
    purchased_credit_applied_nanos: Nanos
    purchased_redemptions_booked_nanos: Nanos
    waived_nanos: Nanos
    pending_meter_nanos: Nanos
    abandoned_meter_nanos: Nanos
    crossing_segments: int
    boundary_overlap_gross_nanos: Nanos
    statement: EconomicsStatement | None
    net_revenue_nanos: int | None
    contribution_nanos: int | None
    operating_result_nanos: int | None
    contribution_margin: Decimal | None
    operating_margin: Decimal | None
    missing_components: list[StatementComponent]
    reconciliation_gaps: list[str]
    occupancy: OccupancyReport

    @property
    def exit_code(self) -> int:
        if self.status is EconomicsStatus.Incomplete:
            return 2
        return (
            1
            if self.status in {EconomicsStatus.ContributionLoss, EconomicsStatus.OperatingLoss}
            else 0
        )


class EconomicsService:
    def __init__(self, database: DatabaseClient) -> None:
        self.database = database

    def report(
        self,
        period: EconomicsPeriod,
        *,
        statement: EconomicsStatement | None = None,
        operations: OperationalStatement | None = None,
    ) -> EconomicsReport:
        for evidence in (statement, operations):
            if evidence is not None and (
                evidence.started_at != period.started_at
                or evidence.ended_at != period.ended_at
                or evidence.scope != period.scope
            ):
                raise InvalidInputError("statement period and scope must exactly match the report")
        with self.database.session() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            facts = EconomicsRepository(session).read(
                started_at=period.started_at,
                ended_at=period.ended_at,
            )
        return _report(period, facts, statement, operations)


def _report(
    period: EconomicsPeriod,
    facts: EconomicsLedgerFacts,
    statement: EconomicsStatement | None,
    operations: OperationalStatement | None,
) -> EconomicsReport:
    amounts = (
        {entry.component: entry.amount_nanos for entry in statement.entries} if statement else {}
    )
    missing = [component for component in StatementComponent if component not in amounts]
    gross = sum(item.gross_nanos for item in facts.usage)
    trial = facts.credits.get(CreditKind.Trial, 0)
    subscription = facts.credits.get(CreditKind.Subscription, 0)
    gaps: list[str] = []
    for count, description in (
        (facts.unpriced_records, "billable records created in this period remain unpriced"),
        (facts.unsettled_records, "usage records lack settled credit attribution"),
        (facts.legacy_segments, "legacy segments lack local credit attribution"),
        (facts.settlement_disagreements, "credit settlements disagree with ledger allocations"),
        (
            facts.missing_delivery_records,
            "payable segments lack retained payment delivery evidence",
        ),
    ):
        if count:
            gaps.append(f"{count} {description}")
    if period.ended_at > datetime.now(UTC):
        gaps.append("the report period has not closed")
    if facts.pending_meter_nanos:
        gaps.append("metered charges are still waiting for payment-provider delivery")
    writeoffs = amounts.get(StatementComponent.RefundsAndWriteoffs)
    if writeoffs is not None and writeoffs < facts.abandoned_meter_nanos:
        gaps.append("abandoned metering exceeds the reconciled refunds and writeoffs")
    control = amounts.get(StatementComponent.GrossUsageControl)
    if control is not None and control != gross:
        gaps.append("statement gross usage does not reconcile to the ledger")
    cash = (
        StatementComponent.PrepaidOpeningLiability,
        StatementComponent.PrepaidCashReceived,
        StatementComponent.PrepaidCashRefunded,
        StatementComponent.PrepaidClosingLiability,
    )
    if all(component in amounts for component in cash) and (
        amounts[cash[0]]
        + amounts[cash[1]]
        - amounts[cash[2]]
        - facts.purchased_redemptions_booked_nanos
        != amounts[cash[3]]
    ):
        gaps.append("prepaid cash, booked redemptions and closing liability do not reconcile")
    if trial + subscription + facts.waived_nanos > gross:
        gaps.append("credits and waivers exceed gross usage")
    revenue = contribution = operating = None
    contribution_margin = operating_margin = None
    status = EconomicsStatus.Incomplete
    if not missing and not gaps:
        revenue = (
            gross
            - trial
            - subscription
            - facts.waived_nanos
            + amounts[StatementComponent.SubscriptionRevenue]
            - amounts[StatementComponent.RefundsAndWriteoffs]
        )
        variable = sum(
            amounts[component]
            for component in (
                StatementComponent.SupplierCompute,
                StatementComponent.ObjectStorage,
                StatementComponent.ObjectOperations,
                StatementComponent.Network,
                StatementComponent.PaymentFees,
            )
        )
        contribution = revenue - variable
        operating = contribution - amounts[StatementComponent.FixedInfrastructure]
        status = (
            EconomicsStatus.ContributionLoss
            if contribution < 0
            else EconomicsStatus.OperatingLoss
            if operating < 0
            else EconomicsStatus.BreakEven
            if operating == 0
            else EconomicsStatus.Profitable
        )
        if revenue > 0:
            contribution_margin = Decimal(contribution) / Decimal(revenue)
            operating_margin = Decimal(operating) / Decimal(revenue)
    return EconomicsReport(
        **period.model_dump(),
        observed_at=datetime.now(UTC),
        status=status,
        usage=[
            UsageRevenue(
                billing_owner=item.billing_owner,
                component=item.component,
                basis=item.basis,
                quantity=item.quantity,
                gross_nanos=item.gross_nanos,
            )
            for item in facts.usage
        ],
        gross_usage_nanos=gross,
        trial_credit_applied_nanos=trial,
        subscription_credit_applied_nanos=subscription,
        purchased_credit_applied_nanos=facts.credits.get(CreditKind.Purchased, 0),
        purchased_redemptions_booked_nanos=facts.purchased_redemptions_booked_nanos,
        waived_nanos=facts.waived_nanos,
        pending_meter_nanos=facts.pending_meter_nanos,
        abandoned_meter_nanos=facts.abandoned_meter_nanos,
        crossing_segments=facts.crossing_segments,
        boundary_overlap_gross_nanos=facts.boundary_overlap_gross_nanos,
        statement=statement,
        net_revenue_nanos=revenue,
        contribution_nanos=contribution,
        operating_result_nanos=operating,
        contribution_margin=contribution_margin,
        operating_margin=operating_margin,
        missing_components=missing,
        reconciliation_gaps=gaps,
        occupancy=_occupancy(facts, operations),
    )


def _occupancy(
    facts: EconomicsLedgerFacts, observations: OperationalStatement | None
) -> OccupancyReport:
    quantities = {
        component: sum(
            (
                item.quantity
                for item in facts.usage
                if (
                    item.billing_owner == UsageBillingOwner.PlatformFleet.value
                    and item.basis is LedgerBasis.Reserved
                    and item.component is component
                )
            ),
            Decimal(0),
        )
        for component in (LedgerComponent.Cpu, LedgerComponent.Memory, LedgerComponent.Gpu)
    }
    reserved = ResourceSeconds(
        cpu_core_seconds=quantities[LedgerComponent.Cpu],
        memory_gib_seconds=quantities[LedgerComponent.Memory],
        gpu_card_seconds=quantities[LedgerComponent.Gpu],
    )
    fields = (
        "capacity",
        "reserved_capacity",
        "stranded_capacity",
        "provisioned_node_seconds",
        "idle_node_seconds",
        "interruption_count",
        "interruption_recovery_seconds",
    )
    gaps = [name for name in fields if observations is None or getattr(observations, name) is None]
    ratios: list[Decimal | None] = [None, None, None]
    if (
        observations is not None
        and observations.capacity is not None
        and observations.reserved_capacity is not None
    ):
        for index, (used, capacity) in enumerate(
            zip(
                _resource_values(observations.reserved_capacity),
                _resource_values(observations.capacity),
                strict=True,
            )
        ):
            ratios[index] = used / capacity if capacity else None
    return OccupancyReport(
        billed_reservations=reserved,
        observations=observations,
        cpu_reservation_ratio=ratios[0],
        memory_reservation_ratio=ratios[1],
        gpu_reservation_ratio=ratios[2],
        gaps=gaps,
    )


def _resource_values(value: ResourceSeconds) -> tuple[Decimal, Decimal, Decimal]:
    return value.cpu_core_seconds, value.memory_gib_seconds, value.gpu_card_seconds

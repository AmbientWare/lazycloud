from __future__ import annotations

from datetime import date, datetime

from pydantic import ConfigDict, Field, model_validator

from shared.billing_accounts import BillingPlan
from shared.contracts import ContractModel
from shared.enums import StringEnum


class BillingPeriodStatus(StringEnum):
    """How far a period has got towards being paid.

        Usage lands in an open period; closing freezes what it owes; issuing hands
        it to the payment provider as an invoice.

        `Invoiced` is not `Paid`. Finalizing an invoice states what is owed; the money
        arriving is a separate event, learned from the provider rather than decided
        here, and a period sits at `Invoiced` for as long as that takes.

    `PaymentFailed` is a period state and not only an account one, because standing
        is a fact about an account's *months* and has to be derivable from them. An
        account with a failed September and a paid October is behind; deciding that
        from whichever notification arrived last gets it right only if they arrive in
        order, and they do not — deliveries are retried for days and overtake each
        other. Kept here, the question is answered by reading the periods.

        `Voided` is how a month stops being owed without being paid. An invoice can
        only be withdrawn, never deleted, and a period left pointing at a withdrawn
        invoice would either sit `Invoiced` forever or hold an account behind for
        money nobody intends to collect.

        `NothingOwed` is the other way a period finishes: a free account that stayed
        inside its allowance owes nothing, and an invoice for zero is a document
        somebody still has to read. Kept distinct from `Invoiced` because the two are
        different facts — one has an invoice behind it and the other has none — and
        because a period with no terminal state of its own is one every later sweep
        picks up again forever.
    """

    Open = "open"
    Closed = "closed"
    Invoiced = "invoiced"
    NothingOwed = "nothing_owed"
    Paid = "paid"
    PaymentFailed = "payment_failed"
    Voided = "voided"


class BillingPeriod(ContractModel):
    """One account's bill for one month.

    Scoped to the payer rather than to a workspace: an account holds every
    workspace its owner owns and pays once, so a period sums their days.

    Everything it will be charged for is frozen onto it when it closes — what
    the plan cost, what it included, what the usage came to. A price change
    afterwards moves the next period, never this one, which is the whole
    difference between this and the ledger it was built from.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    user_id: str
    period_start: date
    period_end: date
    """Exclusive, so a month's periods tile without overlapping."""

    status: BillingPeriodStatus = BillingPeriodStatus.Open
    plan: BillingPlan = BillingPlan.Free
    currency: str = Field(min_length=3, max_length=3)
    provider_invoice_id: str = Field(default="", max_length=255)
    """The invoice stating what this period owes, empty until one is issued. An
    amount nobody can point at a document is one nobody can dispute or
    reconcile."""

    usage_cost_nanos: int = Field(default=0, ge=0)
    included_cost_nanos: int = Field(default=0, ge=0)
    subscription_cost_nanos: int = Field(default=0, ge=0)
    charged_cost_nanos: int = Field(default=0, ge=0)
    payment_attempted_at: datetime | None = None
    """When collection was last tried. Null where it never has been, which is not
    the same as tried and failed — one is a month waiting its turn, the other a
    customer whose card refused, and the retry policy treats them differently."""

    @model_validator(mode="after")
    def invoiced_periods_name_their_invoice(self) -> BillingPeriod:
        if (
            self.status
            in {
                BillingPeriodStatus.Invoiced,
                BillingPeriodStatus.Paid,
                BillingPeriodStatus.PaymentFailed,
                BillingPeriodStatus.Voided,
            }
            and not self.provider_invoice_id
        ):
            raise ValueError("an invoiced period must name its invoice")
        return self

    @model_validator(mode="after")
    def periods_end_after_they_start(self) -> BillingPeriod:
        if self.period_end <= self.period_start:
            raise ValueError("a billing period must end after it starts")
        return self


__all__ = ["BillingPeriod", "BillingPeriodStatus"]

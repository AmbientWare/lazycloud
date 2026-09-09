from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from uuid import uuid4

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_plan_changes import (
    BillingPlanChangeIntentRepository,
    ClaimedPlanChange,
)
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import published_plan
from shared.errors import (
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
    UpstreamUnavailableError,
)
from shared.events import EventLevel
from shared.payments import ProviderSubscription, SubscriptionPaymentProvider, SubscriptionProration
from shared.timestamps import to_utc, utc_now
from sqlalchemy.orm import Session

from billing.accounts import BillingAccountService, owned_workspace_id
from billing.admission import DatabaseBillingAdmission
from billing.periods import carry_plan_into_cycle
from billing.sweeps import BillingEventSink, next_attempt_at
from billing.webhooks import ENDED_SUBSCRIPTION_STATUSES

LOGGER = logging.getLogger(__name__)

PLAN_CHANGE_ABANDONED_ACTION = "billing.plan_change.abandoned"
"""A plan change nobody could settle.

Either its outcome could not be established, or it was established and is not
one this platform may write down — a change collected on a subscription that has
since ended is money taken for terms nothing can be given.
"""

PLAN_CHANGE_RESOURCE_TYPE = "billing_plan_change"

MAX_ATTEMPTS = 20
"""Attempts an intent is given before its outcome is given up on.

With the backoff below this spans about seven hours. Nothing bounds it from the
provider's side the way the meter outbox is bounded — reading a subscription is
idempotent and carries no deduplication window — so what it is sized against is
how long an account may sit holding a plan nobody here has decided about.
"""

CLAIM_TTL = timedelta(minutes=5)
"""How long a settler holds an intent before the sweep may take it.

Also the grace the request that opened the intent gets to finish its own
provider call: the intent is inserted already claimed, so this is what keeps the
sweep from reading a subscription the swap is still in flight against.
"""

_RETRY_BASE = timedelta(seconds=30)
_RETRY_CAP = timedelta(seconds=1_800)


@dataclass(frozen=True, slots=True)
class PlanChangeSettleResult:
    """What one sweep decided, and how much is still undecided."""

    applied_count: int = 0
    not_applied_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0
    open_count: int = 0


class _Verdict(Enum):
    Applied = auto()
    NotApplied = auto()
    Unresolved = auto()
    """The provider holds the plan and nothing here may record it.

    Terminal like the other two, and the only one that costs an operator
    something: the money may have been taken, and the subscription it was taken
    on is not one this account can be given.
    """


@dataclass(frozen=True, slots=True)
class _Settlement:
    account: BillingAccount
    verdict: _Verdict
    settled: bool
    """Whether the outcome reached the intent, false where the claim was lost.

    A settler whose claim went stale mid-flight still writes the account row —
    it holds that row's lock and writes what the provider says — but the intent
    belongs to whoever reclaimed it. Counting an outcome this settler did not
    record would report a change as finished while it is still open, and the
    figure an operator watches for changes nobody decided is exactly that count.
    """

    reason: str = ""


@dataclass(frozen=True, slots=True)
class BillingPlanChangeService:
    """Move an account between published plans, and finish the moves that stopped.

    The provider cannot join a transaction here, so a plan change is recorded
    before it is attempted and settled from what the provider says afterwards.
    That is the whole of the design: moving onto dearer terms raises and collects
    the proration inside `set_subscription_plan`, so anything failing between the
    charge and the account row naming the new plan leaves a customer who has paid
    for a plan this platform still judges them off. With the intent row there is
    something to find, and the subscription itself is what answers it.

    Asking the provider settles it definitively rather than probably, though what
    the answer proves is directional. Upwards, the swap is sent refusing to
    complete unpaid, so the subscription carries the new price only if the money
    was taken: "the item is on this plan" and "the difference was collected" are
    one fact, readable in one request. Downwards there is nothing to collect —
    the cycle was invoiced when it opened and stays invoiced — so the plan being
    there is the whole of what happened.

    Moving down is a price swapped back, never a subscription ended. Ending one
    clears the metered prices with it, and this account's usage would go on
    running and reach no invoice at all; the account row is cleared for exactly
    that state, so new work would then be refused as well. The subscription id,
    the anniversary and the three metered items survive every direction.

    What the provider's answer does not say is whether the subscription is still
    the one this account is on, and a plan carries no meaning apart from the
    subscription holding it. So a change is written back only onto a live
    subscription the account still names, and one that has ended underneath it
    is kept as evidence for an operator instead of handed to an account as terms
    nothing bills.
    """

    database: DatabaseClient
    payments: Callable[[], SubscriptionPaymentProvider]
    events: BillingEventSink
    batch_limit: int = 100

    def change_plan(self, *, user_id: str, target: BillingPlanId) -> BillingAccount:
        """Move this account onto a published plan, in either direction.

        A price swapped on the subscription it already holds, not a second
        subscription and never a cancellation: the identifier, the billing
        anniversary and the three metered items survive, so the usage already
        recorded this cycle stays where it is and is billed on the invoice it
        belongs to.

        The open period is re-termed in place rather than replaced, keeping what
        has been spent against it, and the grant that funded the smaller
        allowance is expired so that one grant covers the cycle. A customer
        neither loses the allowance they already had nor holds both. Moving onto
        cheaper terms re-terms nothing at all — a period never takes a smaller
        allowance than the one it opened on — so the cycle keeps what it was
        bought with, the grant funding it is left alone, and the smaller plan
        applies from the next cycle. The customer is charged neither more nor
        less for the month already invoiced.

        A change landing in the seam between a cycle ending and its invoice
        finalizing is a different act, and `carry_plan_into_cycle` is what tells
        them apart: the cycle the provider answers with is a new one, so the
        outgoing grant is left to fund the invoice it was bought for rather than
        voided out from under it.

        A refusal is not an answer, so it settles nothing on its own: the
        subscription is read back, and only a provider that already holds the
        plan decides the change inside this request. Anything else is paced for
        the sweep to ask again.

        An account already on the target costs no intent and reaches no provider,
        so the second of two clicks changes nothing and cannot collide with the
        first.

        A monthly plan is refused here rather than at the provider when nobody
        can be charged for it. The provider would refuse it too — the swap is
        sent `error_if_incomplete`, so a proration nobody can pay cannot complete
        — but that refusal arrives after the intent has been committed, and a
        committed intent is expensive to leave behind: it holds the account's one
        open-change slot until its claim expires, is retried on a schedule that
        costs a provider read each time, and ends in an error-level abandonment
        that asks an operator to account for money which never moved. Refusing
        before the row is written turns all of that into one answer the customer
        can act on. It keys on the target's price rather than on which plan it
        is, so a move *off* a paid plan is never refused for want of a card —
        somebody whose card has gone is exactly who needs that move.
        """

        # Asked before the provider is even resolved, let alone provisioning,
        # which registers a customer there: a request that is refused must not
        # leave one behind, and must not need a provider to be refused.
        with self.database.session() as session:
            waived = BillingAccountRepository(session).get_by_user(user_id)
        if waived is not None and waived.complimentary_since is not None:
            raise ConflictError("this account's usage is complimentary; it holds no plan to change")
        payments = self.payments()
        claim_token = str(uuid4())
        with self.database.session() as session:
            account = BillingAccountService(session).billing_account_for(
                payments,
                user_id=user_id,
                workspace_id=owned_workspace_id(session, user_id),
            )
            if account.plan is target:
                return account
            DatabaseBillingAdmission().assert_plan_change_fits(
                session,
                user_id=user_id,
                target=target,
            )
            if (
                published_plan(target).monthly_nanos > 0
                and account.payment_method_attached_at is None
            ):
                raise PaymentRequiredError(
                    "this plan is billed monthly and cannot be started without a card; "
                    "add a payment method and subscribe again"
                )
            # What the cycle was already invoiced at, not what the account is on
            # now. A move down leaves the period holding the terms it was paid
            # for, so moving back up inside the same cycle is a reversal rather
            # than a purchase — and charging it as a purchase would take the
            # prorated month a second time, since the move down credited nothing.
            paid_for_nanos = _allowance_paid_for(session, user_id=user_id)
            proration = _proration_onto(
                target, from_plan=account.plan, paid_for_nanos=paid_for_nanos
            )
            intent = BillingPlanChangeIntentRepository(session).open(
                user_id=user_id,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id=account.provider_subscription_id,
                target_plan=target,
                now=utc_now(),
                claim_token=claim_token,
            )
        try:
            subscription = payments.set_subscription_plan(
                provider_subscription_id=intent.provider_subscription_id,
                plan=intent.target_plan,
                proration=proration,
            )
        except Exception:
            settlement = self._settle_after_refusal(payments, intent, claim_token=claim_token)
            if settlement is None or settlement.verdict is not _Verdict.Applied:
                raise
            # The swap landed despite the refusal, which is what carrying the
            # plan proves. Reporting the failure would tell a customer who has
            # already been moved — and, upwards, already paid — that neither
            # happened.
            return settlement.account
        return self._decide(
            payments, intent, claim_token=claim_token, subscription=subscription
        ).account

    def settle_open(self, *, now: datetime | None = None) -> PlanChangeSettleResult:
        """Finish the plan changes whose outcome nobody recorded.

        Every intent is one question to the provider: does the subscription
        carry the plan the change was for? It does, so the cycle and the row are
        given those terms — and where the move was upwards, the provider holding
        the plan is also what says the difference was collected. It does not, and
        nothing happened, so nothing is written.

        A third answer is neither, and it is the one an operator has to see: the
        plan is there on a subscription that has ended, or on one this account is
        no longer on. Money may have been taken and there are no terms to give
        for it, so the intent is kept as the evidence and the disagreement is
        recorded rather than resolved.

        The credential is resolved before anything is claimed, because a claim
        spends an attempt and no number of retries fixes a key this process
        cannot read.
        """

        moment = to_utc(now or utc_now())
        payments = self.payments()
        with self.database.session() as session:
            BillingPlanChangeIntentRepository(session).reclaim(
                now=moment,
                claimed_before=moment - CLAIM_TTL,
            )
        claim_token = str(uuid4())
        with self.database.session() as session:
            claimed = BillingPlanChangeIntentRepository(session).claim(
                now=moment,
                limit=self.batch_limit,
                claim_token=claim_token,
            )
        applied = not_applied = retried = abandoned = 0
        exhausted: list[tuple[ClaimedPlanChange, str]] = []
        for intent in claimed:
            try:
                settlement = self._decide(
                    payments, intent, claim_token=claim_token, subscription=None
                )
            except Exception as error:
                LOGGER.warning(
                    "billing: plan change %s is still undecided (attempt %d): %s",
                    intent.id,
                    intent.attempts,
                    error,
                )
                if intent.attempts >= MAX_ATTEMPTS:
                    reason = f"gave up after {intent.attempts} attempts: {error}"
                    if self._abandon(intent, claim_token=claim_token, now=moment, error=reason):
                        exhausted.append((intent, reason))
                    continue
                retried += int(
                    self._retry(intent, claim_token=claim_token, now=moment, error=str(error))
                )
                continue
            if not settlement.settled:
                # Reclaimed while the provider was being asked. The account row
                # is right either way, and the intent is now somebody else's to
                # decide and to count.
                continue
            if settlement.verdict is _Verdict.Applied:
                applied += 1
            elif settlement.verdict is _Verdict.NotApplied:
                not_applied += 1
            else:
                abandoned += 1
        # Outside the transactions that settled them: the sink opens its own
        # session, and an event recorded from inside one would claim a
        # settlement that has not committed.
        for intent, reason in exhausted:
            self._record_abandonment(intent, reason)
        with self.database.session() as session:
            open_count = BillingPlanChangeIntentRepository(session).open_count()
        return PlanChangeSettleResult(
            applied_count=applied,
            not_applied_count=not_applied,
            retried_count=retried,
            abandoned_count=abandoned + len(exhausted),
            open_count=open_count,
        )

    def _settle_after_refusal(
        self, payments: SubscriptionPaymentProvider, intent: ClaimedPlanChange, *, claim_token: str
    ) -> _Settlement | None:
        """Decide a refused change only where the provider already holds the plan.

        A refusal says nothing certain: `error_if_incomplete` can fail after the
        invoice was raised, and a read taken milliseconds behind a call that
        timed out cannot tell a provider that never moved from one still moving.
        Closing the intent on that read is the one outcome nothing recovers from
        — a swap that lands afterwards is a charge with no record that anybody
        meant it — so the plan already being there is the only answer taken here.

        Everything else is paced instead: the sweep asks again once the provider
        has stopped moving, and a customer whose card was refused gets their
        button back a schedule later rather than a conflict forever.
        """

        moment = utc_now()
        try:
            held = payments.subscription(provider_subscription_id=intent.provider_subscription_id)
            if held.plan is intent.target_plan:
                return self._decide(payments, intent, claim_token=claim_token, subscription=held)
            outcome = f"the provider does not hold the {intent.target_plan.value} plan"
        except Exception as error:
            outcome = f"the provider would not say what it holds: {error}"
        LOGGER.info("billing: plan change %s is left to the sweep: %s", intent.id, outcome)
        self._retry(intent, claim_token=claim_token, now=moment, error=outcome)
        return None

    def _decide(
        self,
        payments: SubscriptionPaymentProvider,
        intent: ClaimedPlanChange,
        *,
        claim_token: str,
        subscription: ProviderSubscription | None,
    ) -> _Settlement:
        """Settle one intent, and report an outcome nobody here can act on.

        The event is recorded outside the transaction that settled the intent:
        the sink opens its own session, and one written from inside would claim
        a settlement that has not committed.
        """

        settlement = self._settle(
            payments, intent, claim_token=claim_token, subscription=subscription
        )
        if settlement.verdict is _Verdict.Unresolved and settlement.settled:
            self._record_abandonment(intent, settlement.reason)
        return settlement

    def _settle(
        self,
        payments: SubscriptionPaymentProvider,
        intent: ClaimedPlanChange,
        *,
        claim_token: str,
        subscription: ProviderSubscription | None,
    ) -> _Settlement:
        """Decide one intent from the subscription, and write what it decided.

        The account row lock is taken before anything is decided, which is what
        `carry_plan_into_cycle` requires of every caller and what makes two
        settlers safe: the second waits, finds the cycle already on these terms,
        and buys nothing.

        The plan written down is the subscription's rather than the intent's,
        the same rule a delivery follows. The two agree here by construction —
        that is what makes this branch the applied one — and reading it off the
        provider is what keeps one place deciding what an account is on.

        Two things stop a change being recorded even where the provider holds
        the plan, and they are one fact twice: the subscription the money was
        taken on is not the subscription this account is on. A provider that
        says it has ended, or an account since provisioned onto another one,
        would otherwise be written back onto the row along with a fresh
        allowance bought against a dead cycle — a plan and a subscription
        admission reads as live while the usage they admit reaches no invoice,
        which is the state this sweep exists to prevent rather than to create.
        Neither is decidable from here, so the intent is kept as the evidence
        and an operator is told.
        """

        held = subscription or payments.subscription(
            provider_subscription_id=intent.provider_subscription_id
        )
        moment = utc_now()
        with self.database.session() as session:
            accounts = BillingAccountRepository(session)
            intents = BillingPlanChangeIntentRepository(session)
            account = accounts.get_by_user(intent.user_id, for_update=True)
            if account is None:
                raise NotFoundError(f"no billing account to settle a plan change for: {intent.id}")
            if held.plan is None:
                # A licensed price this platform did not publish. Whether the
                # change happened is not answerable from it, and neither is what
                # the cycle would be worth, so the intent stays open.
                raise UpstreamUnavailableError(
                    f"the payment provider holds subscription {intent.provider_subscription_id} "
                    "on a price this platform did not publish, so there is no plan change to "
                    "settle"
                )
            if held.plan is not intent.target_plan:
                return _Settlement(
                    account=account,
                    verdict=_Verdict.NotApplied,
                    settled=intents.mark_not_applied(
                        intent_id=intent.id, claim_token=claim_token, now=moment
                    ),
                )
            if held.status in ENDED_SUBSCRIPTION_STATUSES:
                return self._unresolved(
                    intents,
                    intent,
                    account,
                    claim_token=claim_token,
                    now=moment,
                    reason=(
                        f"subscription {intent.provider_subscription_id} reads {held.status}, so "
                        "the plan it was moved onto is not one this account holds"
                    ),
                )
            if account.provider_subscription_id != intent.provider_subscription_id:
                return self._unresolved(
                    intents,
                    intent,
                    account,
                    claim_token=claim_token,
                    now=moment,
                    reason=(
                        f"the change was made on subscription "
                        f"{intent.provider_subscription_id}, which this account is no longer on"
                    ),
                )
            grant_id = carry_plan_into_cycle(
                session,
                payments,
                account_id=account.user_id,
                provider_customer_id=account.provider_customer_id,
                provider_credit_grant_id=account.provider_credit_grant_id,
                subscription=held,
                plan=held.plan,
                has_payment_method=account.payment_method_attached_at is not None,
            )
            written = accounts.upsert(
                user_id=account.user_id,
                status=account.status,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id=held.provider_subscription_id,
                provider_credit_grant_id=grant_id,
                plan=held.plan,
            )
            return _Settlement(
                account=written,
                verdict=_Verdict.Applied,
                settled=intents.mark_applied(
                    intent_id=intent.id, claim_token=claim_token, now=moment
                ),
            )

    def _unresolved(
        self,
        intents: BillingPlanChangeIntentRepository,
        intent: ClaimedPlanChange,
        account: BillingAccount,
        *,
        claim_token: str,
        now: datetime,
        reason: str,
    ) -> _Settlement:
        """Stop asking about a change nothing here may record, and say why."""

        return _Settlement(
            account=account,
            verdict=_Verdict.Unresolved,
            settled=intents.abandon(
                intent_id=intent.id, claim_token=claim_token, now=now, error=reason
            ),
            reason=reason,
        )

    def _retry(
        self, intent: ClaimedPlanChange, *, claim_token: str, now: datetime, error: str
    ) -> bool:
        with self.database.session() as session:
            return BillingPlanChangeIntentRepository(session).mark_failed(
                intent_id=intent.id,
                claim_token=claim_token,
                now=now,
                next_attempt_at=next_attempt_at(
                    now,
                    intent.attempts,
                    base=_RETRY_BASE,
                    cap=_RETRY_CAP,
                ),
                error=error,
            )

    def _abandon(
        self, intent: ClaimedPlanChange, *, claim_token: str, now: datetime, error: str
    ) -> bool:
        with self.database.session() as session:
            return BillingPlanChangeIntentRepository(session).abandon(
                intent_id=intent.id,
                claim_token=claim_token,
                now=now,
                error=error,
            )

    def _record_abandonment(self, intent: ClaimedPlanChange, error: str) -> None:
        try:
            self.events.emit(
                PLAN_CHANGE_ABANDONED_ACTION,
                resource_type=PLAN_CHANGE_RESOURCE_TYPE,
                resource_id=intent.id,
                message=(
                    f"a change to the {intent.target_plan.value} plan was attempted and could "
                    f"not be settled: {error}"
                ),
                level=EventLevel.Error,
                data={
                    "intent_id": intent.id,
                    "user_id": intent.user_id,
                    "provider_subscription_id": intent.provider_subscription_id,
                    "target_plan": intent.target_plan.value,
                    "attempts": intent.attempts,
                    "error": error,
                },
            )
        except Exception:
            # Wrapped so that failing to record the abandonment cannot replace
            # the abandonment as what this process reports.
            LOGGER.exception(
                "billing: plan change %s was abandoned and the event was not recorded",
                intent.id,
            )


def _allowance_paid_for(session: Session, *, user_id: str) -> int:
    """What the cycle in progress is stamped at, or zero where none is open.

    The period row is the record of what this cycle was invoiced for: it is
    written when the cycle opens and re-termed when a plan is bought inside it,
    and a move down deliberately leaves the larger figure standing because the
    customer paid for it. So it answers the one question proration needs and the
    account row cannot — what has already been charged for these days.
    """

    period = BillingAllowanceRepository(session).current_period(user_id=user_id, at=utc_now())
    return period.allowance_nanos if period is not None else 0


def _proration_onto(
    target: BillingPlanId, *, from_plan: BillingPlanId | None, paid_for_nanos: int
) -> SubscriptionProration:
    """What this move does about the stretch of cycle already invoiced.

    Read from the published prices rather than from which plan is which, so a
    plan added to the card is priced into this without the rule being edited.

    Dearer takes the difference now, which is what makes the provider's answer
    proof that the money was collected. Anything else takes nothing and returns
    nothing: the month was invoiced when the cycle opened, the allowance stamped
    on that cycle is the allowance it keeps, and the smaller price is what the
    next invoice asks for. Refunding a part-month instead would hand back money
    for compute the account was free to spend and mostly has.

    An account naming no plan holds no subscription anybody has paid for, so
    moving it onto one that costs money is money not yet taken.

    `paid_for_nanos` is what the cycle in progress is stamped at, which is the
    only record of what these days have already been charged for. Without it a
    customer who moved down and back up inside one cycle pays the prorated month
    twice — the move down credits nothing back, so nothing offsets the second
    charge, and the period is already stamped at the target's terms so no
    allowance is bought with it either.
    """

    if published_plan(target).included_nanos <= paid_for_nanos:
        # These days already carry at least this plan's terms, so there is
        # nothing left to buy for them. Reached by moving down and back up inside
        # one cycle, where the move down took nothing back.
        return SubscriptionProration.KeepWhatWasPaidFor
    paid_monthly_nanos = published_plan(from_plan).monthly_nanos if from_plan is not None else 0
    return (
        SubscriptionProration.ChargeDifferenceNow
        if published_plan(target).monthly_nanos > paid_monthly_nanos
        else SubscriptionProration.KeepWhatWasPaidFor
    )


__all__ = [
    "CLAIM_TTL",
    "MAX_ATTEMPTS",
    "PLAN_CHANGE_ABANDONED_ACTION",
    "PLAN_CHANGE_RESOURCE_TYPE",
    "BillingPlanChangeService",
    "PlanChangeSettleResult",
]

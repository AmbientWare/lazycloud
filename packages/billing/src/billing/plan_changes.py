from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from uuid import uuid4

from database.client import DatabaseClient
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_plan_changes import (
    BillingPlanChangeIntentRepository,
    ClaimedPlanChange,
)
from shared.billing_accounts import BillingAccount
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import published_plan, subscription_terms
from shared.errors import (
    ConflictError,
    NotFoundError,
    PaymentRequiredError,
    UpstreamUnavailableError,
)
from shared.events import EventLevel
from shared.payments import (
    ProviderSubscription,
    SubscriptionChangeTiming,
    SubscriptionPaymentProvider,
)
from shared.timestamps import to_utc, utc_now

from billing.accounts import BillingAccountService, owned_workspace_id
from billing.admission import DatabaseBillingAdmission
from billing.periods import carry_plan_into_cycle
from billing.sweeps import BillingEventSink, next_attempt_at
from billing.webhooks import ENDED_SUBSCRIPTION_STATUSES

LOGGER = logging.getLogger(__name__)

PLAN_CHANGE_ABANDONED_ACTION = "billing.plan_change.abandoned"

PLAN_CHANGE_RESOURCE_TYPE = "billing_plan_change"

MAX_ATTEMPTS = 20

# New intents hold the first claim while their request calls the provider.
CLAIM_TTL = timedelta(minutes=5)

_RETRY_BASE = timedelta(seconds=30)
_RETRY_CAP = timedelta(seconds=1_800)


@dataclass(frozen=True, slots=True)
class PlanChangeSettleResult:
    applied_count: int = 0
    not_applied_count: int = 0
    retried_count: int = 0
    abandoned_count: int = 0
    open_count: int = 0


class _Verdict(Enum):
    Applied = auto()
    NotApplied = auto()
    Unresolved = auto()
    """The provider applied the plan to a subscription the account no longer holds."""


@dataclass(frozen=True, slots=True)
class _Settlement:
    account: BillingAccount
    verdict: _Verdict
    settled: bool
    """False if another settler reclaimed the intent before its outcome was written."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class BillingPlanChangeService:
    """Apply paid upgrades and schedule cheaper terms at renewal."""

    database: DatabaseClient
    payments: Callable[[], SubscriptionPaymentProvider]
    events: BillingEventSink
    batch_limit: int = 100

    def change_plan(
        self, *, user_id: str, target: BillingPlanId, target_terms_version: SubscriptionTermsVersion
    ) -> BillingAccount:
        with self.database.session() as session:
            waived = BillingAccountRepository(session).get_by_user(user_id)
        if waived is not None and waived.complimentary_since is not None:
            raise ConflictError("this account's usage is complimentary; it holds no plan to change")
        payments = self.payments()
        claim_token = str(uuid4())
        with self.database.session() as session:
            account = BillingAccountService(session).billing_account_for(
                payments, user_id=user_id, workspace_id=owned_workspace_id(session, user_id)
            )
            if account.subscription_terms_version is None:
                raise UpstreamUnavailableError("subscription terms await verification")
            keeping_current = (
                account.plan is target
                and account.subscription_terms_version is target_terms_version
            )
            if keeping_current and account.scheduled_terms_version is None:
                return account
            if (
                not keeping_current
                and published_plan(target).terms_version is not target_terms_version
            ):
                raise ConflictError("these subscription terms are no longer offered")
            if subscription_terms(target_terms_version).plan is not target:
                raise ConflictError("subscription terms do not belong to this plan")
            timing = _change_timing(account.subscription_terms_version, target_terms_version)
            if timing is SubscriptionChangeTiming.Immediate and not keeping_current:
                DatabaseBillingAdmission().assert_plan_change_fits(
                    session, user_id=user_id, target=target
                )
            if (
                not keeping_current
                and timing is SubscriptionChangeTiming.Immediate
                and subscription_terms(target_terms_version).monthly_nanos > 0
                and account.payment_method_attached_at is None
            ):
                raise PaymentRequiredError(
                    "add a payment method before upgrading your subscription"
                )
            if (
                not keeping_current
                and timing is SubscriptionChangeTiming.Immediate
                and subscription_terms(target_terms_version).monthly_nanos > 0
            ):
                cutover = BillingCreditRepository(session).cutover(user_id=user_id)
                if cutover is None or cutover.completed_at is None:
                    raise ConflictError(
                        "subscription credit migration must finish before a paid upgrade"
                    )
            intent = BillingPlanChangeIntentRepository(session).open(
                user_id=user_id,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id=account.provider_subscription_id,
                target_plan=target,
                target_terms_version=target_terms_version,
                now=utc_now(),
                claim_token=claim_token,
            )
        try:
            subscription = self._apply(payments, intent)
        except Exception:
            settlement = self._settle_after_refusal(payments, intent, claim_token=claim_token)
            if settlement is None or settlement.verdict is not _Verdict.Applied:
                raise
            return settlement.account
        return self._decide(
            payments, intent, claim_token=claim_token, subscription=subscription
        ).account

    def _apply(
        self, payments: SubscriptionPaymentProvider, intent: ClaimedPlanChange
    ) -> ProviderSubscription:
        with self.database.session() as session:
            account = BillingAccountRepository(session).get_by_user(intent.user_id)
        if (
            account is None
            or account.provider_subscription_id != intent.provider_subscription_id
            or account.provider_customer_id != intent.provider_customer_id
        ):
            raise ConflictError("the plan-change subscription is no longer held by this account")
        if account.subscription_terms_version is None:
            raise UpstreamUnavailableError("subscription terms await verification")
        return payments.set_subscription_plan(
            provider_subscription_id=intent.provider_subscription_id,
            terms_version=intent.target_terms_version,
            timing=_change_timing(account.subscription_terms_version, intent.target_terms_version),
            operation_id=intent.id,
            operation_created_at=intent.created_at,
        )

    def settle_open(self, *, now: datetime | None = None) -> PlanChangeSettleResult:
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
                    payments,
                    intent,
                    claim_token=claim_token,
                    subscription=self._apply(payments, intent),
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
                continue
            if settlement.verdict is _Verdict.Applied:
                applied += 1
            elif settlement.verdict is _Verdict.NotApplied:
                not_applied += 1
            else:
                abandoned += 1
        # The sink opens its own transaction, so emit only after settlement commits.
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
        moment = utc_now()
        try:
            held = payments.subscription(provider_subscription_id=intent.provider_subscription_id)
            if _holds_target(held, intent):
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
                raise UpstreamUnavailableError(
                    f"the payment provider holds subscription {intent.provider_subscription_id} "
                    "on a price this platform did not publish, so there is no plan change to "
                    "settle"
                )
            if not _holds_target(held, intent):
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
            )
            written = accounts.upsert(
                user_id=account.user_id,
                status=account.status,
                provider_customer_id=account.provider_customer_id,
                provider_subscription_id=held.provider_subscription_id,
                provider_credit_grant_id=grant_id,
                plan=held.plan,
                subscription_terms_version=held.terms_version,
                scheduled_terms_version=held.scheduled_terms_version,
                scheduled_change_at=held.scheduled_change_at,
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
            # An event-sink failure must not undo the recorded abandonment.
            LOGGER.exception(
                "billing: plan change %s was abandoned and the event was not recorded",
                intent.id,
            )


def _change_timing(
    held: SubscriptionTermsVersion, target: SubscriptionTermsVersion
) -> SubscriptionChangeTiming:
    return (
        SubscriptionChangeTiming.AtRenewal
        if subscription_terms(target).monthly_nanos < subscription_terms(held).monthly_nanos
        else SubscriptionChangeTiming.Immediate
    )


def _holds_target(held: ProviderSubscription, intent: ClaimedPlanChange) -> bool:
    return (
        held.terms_version is intent.target_terms_version and held.scheduled_terms_version is None
    ) or (
        held.scheduled_terms_version is intent.target_terms_version
        and held.scheduled_change_at == held.current_period_ended_at
    )


__all__ = [
    "CLAIM_TTL",
    "MAX_ATTEMPTS",
    "PLAN_CHANGE_ABANDONED_ACTION",
    "PLAN_CHANGE_RESOURCE_TYPE",
    "BillingPlanChangeService",
    "PlanChangeSettleResult",
]

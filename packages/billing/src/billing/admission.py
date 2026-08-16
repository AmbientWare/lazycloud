from __future__ import annotations

from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_rate_card import AccountTerms, account_terms
from shared.errors import CapacityLimitReachedError, PaymentRequiredError
from shared.timestamps import utc_now
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class DatabaseBillingAdmission:
    """Whether an account may start more work.

    Asked before a container exists, in the transaction that would create it, so
    a refusal leaves nothing behind — no row, no published change, and nothing
    reserved at a provider. This mirrors how a paused app is refused, and for the
    same reason: a check after the record is written has to undo it, and the
    version of that which runs after a crash never happens at all.

    Only ever refuses *new* work — a container about to start, a volume about to
    exist. Containers already running are stopped, when they are stopped at all,
    by the sweep that watches accounts nobody can be charged for, a decision made
    against the whole account rather than against whichever container happened to
    start next. A volume that already exists is stopped by nothing, which is why
    the refusal is the only place it can be caught.

    The two questions below are split by what they ask, not by what is asking.
    Everything billed asks the first; only a container carries a count, so only a
    container has a second method.

    The first question is still the only one that matters for an account somebody
    can bill: will what this runs reach an invoice somebody is paying? For those
    accounts, how much it costs is not part of it. Every one holds a subscription
    carrying the metered prices, so overage is billed by the provider and chased
    through their card — spending past what a plan includes is something to
    invoice, never something to refuse on.

    An account with no card on file is the case that reasoning does not cover.
    There is no card to chase and no invoice that will ever be paid, so what it
    spends past its allowance is not billed later, it is lost. That is the one
    place an amount decides, and it decides only for accounts in that state:
    attaching a card moves them onto the plan's terms and out of this check for
    good.

    Concurrency is refused separately and differently, because an account at its
    limit owes nothing and paying would not help it. It is a bound on how much a
    single account can have running before anything notices — the metering
    interval means spend is always seen slightly late, and the limit is what
    keeps the size of that blind spot proportional.

    Read from local rows rather than from the provider, because this runs on
    every container start and a network round trip there is a start that fails
    whenever the provider is slow.
    """

    def assert_may_take_on_billed_work(self, session: Session, *, workspace_id: str) -> None:
        """Refuse an account whose next billed thing would reach no invoice.

        Named for the question rather than for what is being created, because the
        answer does not depend on which resource asks. A volume asks it before it
        exists; anything else the platform starts charging for asks the same
        thing and needs no method of its own.

        What this does not cover is worth stating plainly for volumes, which are
        the one billed thing that keeps costing after everything stops. Only
        creation is refused, and only the record: an account that made a volume
        while it still had a fraction of a cent left keeps it, and nothing here
        or anywhere else bounds how large it grows — uploads are not admitted and
        there is no size quota. Reaching data that already exists is deliberately
        not refused, since an account locked out of its own files would be a
        data-loss incident dressed as a billing control. So this shrinks the
        window rather than closing it, and closing it needs either a quota or an
        admission on the write path, neither of which exists yet.
        """

        self._billable_account(session, workspace_id=workspace_id)

    def assert_may_start_container(self, session: Session, *, workspace_id: str) -> None:
        """The question above, plus the one bound that is counted per container."""

        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            return None
        owner_user_id, terms = resolved
        live = ContainerRepository(session).count_live_for_owner(owner_user_id=owner_user_id)
        if live >= terms.max_concurrent_containers:
            raise CapacityLimitReachedError(
                f"this account already has {live} containers running or queued, "
                f"which is the most its plan allows ({terms.max_concurrent_containers})"
            )

    def _billable_account(
        self, session: Session, *, workspace_id: str
    ) -> tuple[str, AccountTerms] | None:
        """Whether this workspace's usage will reach an invoice somebody pays.

        Returns the account and what it is allowed, so the caller that also has a
        count to check does not read the same rows twice. `None` means there was
        no account to judge rather than that one passed.
        """

        owner = WorkspaceMemberRepository(session).owner(workspace_id)
        if owner is None:
            # A workspace with no owner row is reachable by nobody, so there is
            # no account to judge and nothing this can decide.
            return None
        account = BillingAccountRepository(session).get_by_user(owner.user_id)
        if account is None or not account.provider_subscription_id or account.plan is None:
            raise PaymentRequiredError(
                "this account holds no subscription for its usage to be billed on; "
                "sign in again to finish setting it up"
            )
        if account.status is BillingAccountStatus.PastDue:
            raise PaymentRequiredError(
                "a payment for this account did not go through; "
                "update the card on file to start new work"
            )
        has_card = account.payment_method_attached_at is not None
        terms = account_terms(account.plan, has_payment_method=has_card)
        if not has_card:
            spent = BillingAllowanceRepository(session).current_period(
                user_id=owner.user_id, at=utc_now()
            )
            # No period covers this instant only in the seam between a cycle
            # ending at the provider and the delivery that opens the next one
            # here. An account with a card is admitted through it and billed for
            # what it does; one without has no terms to spend against, and
            # admitting on absent terms is the unbounded-free-compute state this
            # whole check exists to make unreachable.
            if spent is None or spent.remaining_nanos <= 0:
                raise PaymentRequiredError(
                    "this account has used the compute it gets without a payment method; "
                    "add a card to keep running work"
                )
        return owner.user_id, terms


__all__ = ["DatabaseBillingAdmission"]

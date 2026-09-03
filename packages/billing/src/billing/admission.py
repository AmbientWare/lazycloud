from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.billing_plan_changes import BillingPlanChangeIntentRepository
from database.repositories.compute import AwsAccountConnectionRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import AccountTerms, PlanEntitlements, account_terms, published_plan
from shared.errors import CapacityLimitReachedError, ConflictError, PaymentRequiredError
from shared.gpu import GPU_ANY, NO_GPU, normalize_gpu_type
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
    keeps the size of that blind spot proportional. Two pools rather than one:
    a container counts against the CPU pool or, when it asks for cards, against
    the GPU pool by the number of cards, so a plan's GPU allowance can never be
    spent on web apps and neither figure has to be read as a share of the other.

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

    def admit_container_start(
        self,
        session: Session,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
    ) -> list[str]:
        """The question above, plus what a container's own shape is bounded by.

        Answers with the models to schedule rather than only yes or no, because
        `any` is a request the plan narrows: a free account asking for whatever
        the platform has must reach the scheduler naming the cards it may hold,
        or the first offer taken would be hardware its plan does not sell it.
        """

        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            # No account to judge, so nothing to narrow either: what was asked
            # for is what gets scheduled.
            return list(gpu)
        owner_user_id, terms = resolved
        containers = ContainerRepository(session)
        if gpu_count == 0 and not gpu:
            live = containers.count_live_cpu_for_owner(owner_user_id=owner_user_id)
            limit = terms.entitlements.max_concurrent_cpu_containers
            if live >= limit:
                raise CapacityLimitReachedError(
                    f"this account already has {live} containers running or queued, "
                    f"which is the most its plan allows ({limit})"
                )
            return []
        models = _admitted_gpu_models(gpu, terms.entitlements)
        held = containers.count_live_gpus_for_owner(owner_user_id=owner_user_id)
        gpu_limit = terms.entitlements.max_concurrent_gpus
        if held + gpu_count > gpu_limit:
            raise CapacityLimitReachedError(
                f"this account already holds {held} GPUs across the containers it is "
                f"running or has queued, and {gpu_count} more would pass the most its "
                f"plan allows ({gpu_limit})"
            )
        return models

    def assert_may_create_workspace(self, session: Session, *, owner_user_id: str) -> None:
        """Refuse an account a workspace beyond what its plan comes with.

        The first workspace is allowed before anything is known about the
        account, because sign-in provisions it before billing exists: gated on
        terms, a new customer's very first workspace would be refused for an
        account that is a few statements away from having a subscription, and
        the sign-in that was meant to create both would leave neither.
        """

        owned = WorkspaceMemberRepository(session).owned_workspace_count(owner_user_id)
        if owned == 0:
            return
        limit = self._account_terms_for_user(
            session, user_id=owner_user_id
        ).entitlements.max_workspaces
        if limit != "unlimited" and owned >= limit:
            raise CapacityLimitReachedError(
                f"this account already owns {owned} workspaces, "
                f"which is the most its plan allows ({limit})"
            )

    def assert_may_add_workspace_member(
        self,
        session: Session,
        *,
        workspace_id: str,
        member_user_id: str | None,
    ) -> None:
        """Whether one more person may join a workspace this account owns.

        `member_user_id` is None when the person has no account yet, which is an
        invitation being sent: there is nobody to already be counted, so the seat
        has to be free outright.
        """
        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            return
        owner_user_id, terms = resolved
        self._assert_no_pending_plan_change(session, user_id=owner_user_id)
        repository = WorkspaceMemberRepository(session)
        if member_user_id is not None and repository.is_member_for_owner(
            owner_user_id=owner_user_id,
            member_user_id=member_user_id,
        ):
            return
        limit = terms.entitlements.max_members
        member_count = repository.distinct_member_count_for_owner(owner_user_id)
        if limit != "unlimited" and member_count >= limit:
            raise CapacityLimitReachedError(
                f"this account already has {member_count} members, "
                f"which is the most its plan allows ({limit})"
            )

    def assert_may_use_connected_cloud(self, session: Session, *, user_id: str) -> None:
        terms = self._account_terms_for_user(session, user_id=user_id)
        if not terms.entitlements.connected_cloud:
            raise PaymentRequiredError("connected cloud accounts require the Team plan")

    def assert_may_use_custom_domains(self, session: Session, *, user_id: str) -> None:
        terms = self._account_terms_for_user(session, user_id=user_id)
        if not terms.entitlements.custom_domains:
            raise PaymentRequiredError("custom domains require the Team plan")

    def assert_plan_change_fits(
        self,
        session: Session,
        *,
        user_id: str,
        target: BillingPlanId,
    ) -> None:
        account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        if account is None:
            raise PaymentRequiredError("this account has not been provisioned for billing")
        entitlements = account_terms(
            target,
            has_payment_method=account.payment_method_attached_at is not None,
        ).entitlements
        violations: list[str] = []
        members = WorkspaceMemberRepository(session)
        workspace_count = members.owned_workspace_count(user_id)
        if entitlements.max_workspaces != "unlimited" and (
            workspace_count > entitlements.max_workspaces
        ):
            violations.append(f"{workspace_count} workspaces (limit {entitlements.max_workspaces})")
        violations.extend(_unofferable_gpu_violations(session, user_id=user_id, target=target))
        member_count = members.distinct_member_count_for_owner(user_id)
        if entitlements.max_members != "unlimited" and member_count > entitlements.max_members:
            violations.append(f"{member_count} members (limit {entitlements.max_members})")
        connection = AwsAccountConnectionRepository(session).get_for_user(user_id)
        if (
            connection is not None
            and not connection.platform_fleet
            and not entitlements.connected_cloud
        ):
            violations.append("a connected cloud account")
        domain_count = CustomDomainRepository(session).count_for_user(user_id)
        if domain_count and not entitlements.custom_domains:
            violations.append(f"{domain_count} custom domains")
        if violations:
            raise ConflictError(
                "this account cannot move to the requested plan while it has "
                + ", ".join(violations)
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
        account = BillingAccountRepository(session).get_by_user(owner.user_id, for_update=True)
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

    def _account_terms_for_user(self, session: Session, *, user_id: str) -> AccountTerms:
        account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        if account is None or not account.provider_subscription_id or account.plan is None:
            raise PaymentRequiredError(
                "this account holds no subscription; sign in again to finish setting it up"
            )
        if account.status is BillingAccountStatus.PastDue:
            raise PaymentRequiredError(
                "a payment for this account did not go through; update the card on file"
            )
        self._assert_no_pending_plan_change(session, user_id=user_id)
        return account_terms(
            account.plan,
            has_payment_method=account.payment_method_attached_at is not None,
        )

    @staticmethod
    def _assert_no_pending_plan_change(session: Session, *, user_id: str) -> None:
        if BillingPlanChangeIntentRepository(session).has_open(user_id=user_id):
            raise ConflictError(
                "this account cannot add plan-limited resources while a plan change is pending"
            )


def _admitted_gpu_models(gpu: Sequence[str], entitlements: PlanEntitlements) -> list[str]:
    """The models a GPU request is to be scheduled with, or a refusal.

    A request for `any` is answered with the plan's own models rather than
    passed through, so the wildcard is resolved once here instead of by every
    pool that later has to decide what an account may be offered. A request that
    names models is answered with those models, because narrowing a stated
    preference would run something other than what was asked for.
    """

    offered = tuple(model.value for model in entitlements.allowed_gpu_types)
    named: list[str] = []
    for entry in gpu:
        normalized = normalize_gpu_type(entry)
        if normalized == NO_GPU:
            continue
        if normalized == GPU_ANY:
            return list(offered)
        if normalized not in offered:
            raise PaymentRequiredError(
                f"this account's plan does not offer {normalized}; it runs "
                f"{', '.join(offered)}. The Team plan runs every model the platform rents."
            )
        named.append(normalized)
    # A count without a model is the wildcard said another way, and the plan
    # narrows it the same.
    return named or list(offered)


def _unofferable_gpu_violations(
    session: Session, *, user_id: str, target: BillingPlanId
) -> list[str]:
    """What this account is running that the plan it is moving to does not sell.

    Counted per model rather than per container, because the model is what the
    customer has to act on: stopping some of six containers means nothing if the
    card under them is the one the plan drops.
    """

    offered = tuple(model.value for model in published_plan(target).entitlements.allowed_gpu_types)
    running: dict[str, int] = {}
    for record in ContainerRepository(session).live_gpu_containers_for_owner(owner_user_id=user_id):
        for entry in record.gpu:
            normalized = normalize_gpu_type(entry)
            # A container that named no model, or asked for whatever was going,
            # is holding a card the target plan can offer by definition.
            if normalized in (NO_GPU, GPU_ANY) or normalized in offered:
                continue
            running[normalized] = running.get(normalized, 0) + 1
            break
    plan_name = published_plan(target).name
    return [
        f"{count} running containers on {model}, which the {plan_name} plan does not offer"
        for model, count in sorted(running.items())
    ]


__all__ = ["DatabaseBillingAdmission"]

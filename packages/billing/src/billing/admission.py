from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_plan_changes import BillingPlanChangeIntentRepository
from database.repositories.compute import AwsAccountConnectionRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import (
    WorkspaceInvitationRepository,
    WorkspaceMemberRepository,
)
from database.repositories.orchestration import ContainerRepository
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import (
    AccountTerms,
    PlanEntitlements,
    account_terms,
    complimentary_terms,
    published_plan,
)
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    PaymentRequiredError,
)
from shared.gpu import GPU_ANY, NO_GPU, normalize_gpu_type
from shared.placement import ProductRegion
from shared.timestamps import utc_now
from sqlalchemy.orm import Session

from billing.preferences import BillingPreferencesService


@dataclass(frozen=True, slots=True)
class DatabaseBillingAdmission:
    """Check account credit and plan entitlements before starting billed work."""

    def assert_may_take_on_billed_work(
        self,
        session: Session,
        *,
        workspace_id: str,
    ) -> None:
        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            raise PaymentRequiredError("billed work requires a workspace billing owner")
        self._assert_funds(session, user_id=resolved[0])

    def _assert_funds(self, session: Session, *, user_id: str) -> None:
        account = BillingAccountRepository(session).get_by_user(user_id)
        if account is not None and account.complimentary_since is not None:
            return
        credits = BillingCreditRepository(session)
        cutover = credits.cutover(user_id=user_id)
        if cutover is None or cutover.completed_at is None:
            raise PaymentRequiredError("credit migration must complete before starting billed work")
        if credits.balance(user_id=user_id, at=utc_now()) <= 0:
            raise PaymentRequiredError("add credit before starting more billed work")
        preferences = BillingPreferencesService(session)
        if preferences.get(user_id=user_id).monthly_usage_limit_nanos is not None:
            budget = preferences.usage_budget(user_id=user_id)
            if budget.available_nanos is not None and budget.available_nanos <= 0:
                raise PaymentRequiredError("the monthly usage limit has been reached")

    def admit_container_start(
        self,
        session: Session,
        *,
        workspace_id: str,
        gpu: Sequence[str],
        gpu_count: int,
        region: ProductRegion | None = None,
        availability_zone: str = "",
    ) -> list[str]:
        """The question above, plus what a container's own shape is bounded by.

        Answers with the models to schedule rather than only yes or no, because
        `any` is a request the plan narrows: a free account asking for whatever
        the platform has must reach the scheduler naming the cards it may hold,
        or the first offer taken would be hardware its plan does not sell it.
        """

        resolved = self._billable_account(session, workspace_id=workspace_id)
        if (
            (region is not None or availability_zone)
            and resolved is not None
            and not resolved[1].entitlements.region_selection
        ):
            raise PaymentRequiredError(
                "region or availability zone selection requires the Team plan"
            )
        if resolved is None:
            raise PaymentRequiredError("billed work requires a workspace billing owner")
        owner_user_id, terms = resolved
        self._assert_funds(session, user_id=owner_user_id)
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
        member_user_id: str,
    ) -> None:
        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            return
        owner_user_id, terms = resolved
        self._assert_no_pending_plan_change(session, user_id=owner_user_id)
        repository = WorkspaceMemberRepository(session)
        if repository.is_member_for_owner(
            owner_user_id=owner_user_id,
            member_user_id=member_user_id,
        ):
            return
        self._assert_seat_free(session, owner_user_id=owner_user_id, terms=terms)

    def assert_may_invite_workspace_member(
        self,
        session: Session,
        *,
        workspace_id: str,
        email: str,
    ) -> None:
        """Whether an offer to this address could be honoured if it were accepted now.

        An open offer holds a seat: five invitations against one free seat would
        send five emails and refuse four people at the door, and the refusal
        should land on the administrator who can act on it. An address already
        seated in one of this owner's workspaces takes no new seat, the same
        allowance adding that account by id gets.
        """
        resolved = self._billable_account(session, workspace_id=workspace_id)
        if resolved is None:
            return
        owner_user_id, terms = resolved
        self._assert_no_pending_plan_change(session, user_id=owner_user_id)
        members = WorkspaceMemberRepository(session)
        if members.member_user_id_for_owner_email(owner_user_id=owner_user_id, email=email):
            return
        open_offers = WorkspaceInvitationRepository(session).open_email_count_for_owner(
            owner_user_id, now=utc_now()
        )
        self._assert_seat_free(
            session, owner_user_id=owner_user_id, terms=terms, held_by_offers=open_offers
        )

    def _assert_seat_free(
        self,
        session: Session,
        *,
        owner_user_id: str,
        terms: AccountTerms,
        held_by_offers: int = 0,
    ) -> None:
        limit = terms.entitlements.max_members
        if limit == "unlimited":
            return
        member_count = WorkspaceMemberRepository(session).distinct_member_count_for_owner(
            owner_user_id
        )
        if member_count + held_by_offers >= limit:
            held = f" and {held_by_offers} open invitations" if held_by_offers else ""
            raise CapacityLimitReachedError(
                f"this account already has {member_count} members{held}, "
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
        if account.complimentary_since is not None:
            raise ConflictError("this account's usage is complimentary; it holds no plan to change")
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
        if account is not None and account.complimentary_since is not None:
            # Nothing this runs is owed, so whether it would reach an invoice is
            # not a question. What is still asked is how much may run at once,
            # which is a bound on the platform's own exposure rather than on a
            # bill, and the Team plan's figure is the one every waived account
            # is held to.
            return owner.user_id, complimentary_terms()
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
        return owner.user_id, terms

    def _account_terms_for_user(self, session: Session, *, user_id: str) -> AccountTerms:
        account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
        if account is not None and account.complimentary_since is not None:
            return complimentary_terms()
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

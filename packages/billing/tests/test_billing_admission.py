from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from database.tables.storage import VolumeTable
from shared.billing_accounts import BillingAccountStatus
from shared.billing_credits import CreditGrant, CreditKind, CreditScope
from shared.billing_plans import BillingPlanId, SubscriptionTermsVersion
from shared.billing_rate_card import FREE_PLAN_GPU_TYPES
from shared.containers import ContainerRecord, ContainerStatus
from shared.custom_domains import CustomDomain
from shared.errors import CapacityLimitReachedError, ConflictError, PaymentRequiredError
from shared.gpu import GPU_ANY, SUPPORTED_GPU_TYPES
from shared.http.volumes import GetOrCreateVolumeRequest
from shared.timestamps import utc_now
from sqlalchemy import func, select
from tests.service_fixtures import legacy_billing_account, unbilled_account, workspace_owner_user_id

from billing import DatabaseBillingAdmission

FUNCTION_IMAGE = "python:3.12-slim"


def test_free_plan_refuses_paid_capabilities(isolated_services: ApiServices) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    admission = DatabaseBillingAdmission()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="connected cloud accounts require"),
    ):
        admission.assert_may_use_connected_cloud(session, user_id=user_id)
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="custom domains require"),
    ):
        admission.assert_may_use_custom_domains(session, user_id=user_id)


def test_the_free_plan_counts_the_owner_as_its_one_member(
    isolated_services: ApiServices,
) -> None:
    """The plan comes with the person who signed up, and nobody else.

    Counted the way the membership rows count, which includes the owner's own,
    so a workspace with no co-members is already at the limit, and wanting to
    work with somebody is what a free account upgrades for.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    colleague = isolated_services.users.create(display_name="member-one")

    with pytest.raises(CapacityLimitReachedError, match="1 members"):
        isolated_services.users.add_member(
            workspace_id=workspace_id,
            user_id=colleague.id,
            admission=DatabaseBillingAdmission(),
        )


def test_an_open_invitation_holds_the_seat_it_would_fill(
    isolated_services: ApiServices,
) -> None:
    """The refusal belongs to the administrator inviting, not the person invited.

    A seat checked only at acceptance lets an owner send more offers than the
    plan can honour: every email goes out, the first acceptance takes the seat,
    and everyone after it is turned away after signing in, by a message about
    somebody else's plan.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(CapacityLimitReachedError, match="1 members"),
    ):
        DatabaseBillingAdmission().assert_may_invite_workspace_member(
            session,
            workspace_id=workspace_id,
            email="colleague@example.test",
        )


def test_plan_change_refuses_to_drop_a_capability_still_in_use(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        CustomDomainRepository(session).create(
            CustomDomain(
                id=str(uuid4()),
                user_id=user_id,
                hostname="quota.example",
            ),
            user_id=user_id,
        )

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ConflictError, match="1 custom domains"),
    ):
        DatabaseBillingAdmission().assert_plan_change_fits(
            session,
            user_id=user_id,
            target=BillingPlanId.Free,
        )


def test_an_account_behind_on_payment_cannot_start_work_and_leaves_no_container(
    isolated_services: ApiServices,
) -> None:
    """Refused before the container exists, not after.

    A check that ran once the row was written would have to undo it, and the
    version of that which runs after a crash never happens at all — leaving a
    pending container nobody scheduled and nobody cleans up. So the refusal is
    taken inside the transaction that would have created it, and the proof is
    that the table is untouched.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        # Fully provisioned, so what refuses is the standing rather than the
        # absence of anything to bill against — the other refusal this asks for.
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.PastDue,
            provider_customer_id="cus_gate",
            provider_subscription_id="sub_gate",
            provider_credit_grant_id="credgr_gate",
            plan=BillingPlanId.Team,
            subscription_terms_version=SubscriptionTermsVersion.Team,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()

    before = _container_count(isolated_services, workspace_id)
    with pytest.raises(PaymentRequiredError, match="did not go through"):
        isolated_services.containers.run(
            "past-due-container",
            FUNCTION_IMAGE,
            ["python", "-m", "runner.function"],
            workspace_id=workspace_id,
        )
    assert _container_count(isolated_services, workspace_id) == before


def test_an_unfunded_account_gets_no_new_volume_but_still_reaches_the_one_it_has(
    isolated_services: ApiServices,
) -> None:
    """An empty balance refuses new storage without hiding existing files."""

    volumes = isolated_services.volume_service
    now = utc_now()
    user_id, workspace_id = legacy_billing_account(
        isolated_services.context,
        period_started_at=now,
        period_ended_at=now + timedelta(days=30),
    )
    with isolated_services.context.database.session() as session:
        credits = BillingCreditRepository(session)
        credits.prepare_cutover(user_id=user_id, effective_at=now)
        credits.complete_cutover(user_id=user_id, at=now)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                "payment:storage-access",
                CreditKind.Purchased,
                CreditScope.AllMetered,
                1_000_000_000,
                utc_now(),
            ),
        )
    existing = volumes.get_or_create_volume(
        GetOrCreateVolumeRequest(name="already-here"),
        workspace_id=workspace_id,
    )
    assert existing.volume is not None
    with isolated_services.context.database.session() as session:
        BillingCreditRepository(session).adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:storage-access",
            amount_nanos=-1_000_000_000,
            effective_at=utc_now(),
        )

    with pytest.raises(PaymentRequiredError, match="add credit"):
        volumes.get_or_create_volume(
            GetOrCreateVolumeRequest(name="one-more"),
            workspace_id=workspace_id,
        )
    # The refusal is asked inside the transaction that would write the row, so
    # the row is what proves it: a check moved after the insert would raise this
    # same error and leave a volume metering behind it.
    assert _volume_names(isolated_services, workspace_id) == ["already-here"]

    resolved = volumes.get_or_create_volume(
        GetOrCreateVolumeRequest(name="already-here"),
        workspace_id=workspace_id,
    )
    assert resolved.volume is not None
    assert resolved.volume.id == existing.volume.id


def _volume_names(services: ApiServices, workspace_id: str) -> list[str]:
    with services.context.database.session() as session:
        return sorted(
            session.scalars(
                select(VolumeTable.name).where(VolumeTable.workspace_id == workspace_id)
            )
        )


def _container_count(services: ApiServices, workspace_id: str) -> int:
    with services.context.database.session() as session:
        return int(
            session.scalar(select(func.count()).where(ContainerTable.workspace_id == workspace_id))
            or 0
        )


def test_a_free_plan_gpu_request_is_held_to_the_models_the_plan_offers(
    isolated_services: ApiServices,
) -> None:
    """A card the plan does not sell is refused; `any` narrows to the ones it does.

    The narrowing is the half that would otherwise be silent. A wildcard passed
    through reaches the scheduler as "whatever is going", and the first offer
    taken would be the hardware this account may not hold, so the plan has to
    answer with its own models rather than only with yes.
    """

    workspace_id = _carded_free_account(isolated_services)
    admission = DatabaseBillingAdmission()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match=r"H100.*Team plan"),
    ):
        admission.admit_container_start(
            session,
            workspace_id=workspace_id,
            gpu=["H100"],
            gpu_count=1,
        )

    with isolated_services.context.database.session() as session:
        assert admission.admit_container_start(
            session,
            workspace_id=workspace_id,
            gpu=[GPU_ANY],
            gpu_count=1,
        ) == [model.value for model in SUPPORTED_GPU_TYPES if model in FREE_PLAN_GPU_TYPES]


def test_the_gpu_limit_counts_cards_and_leaves_the_cpu_pool_alone(
    isolated_services: ApiServices,
) -> None:
    """Cards, not containers, and a pool of their own.

    A container may hold several, so counting containers would let one account
    hold the plan's limit many times over. Counting them against the CPU figure
    instead would let GPU work crowd out the web apps the same plan promises.
    """

    workspace_id = _carded_free_account(isolated_services)
    _hold_gpu_cards(isolated_services, workspace_id=workspace_id, cards=4)
    admission = DatabaseBillingAdmission()

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(CapacityLimitReachedError, match="already holds 4 GPUs"),
    ):
        admission.admit_container_start(
            session,
            workspace_id=workspace_id,
            gpu=["T4"],
            gpu_count=2,
        )

    with isolated_services.context.database.session() as session:
        assert admission.admit_container_start(
            session,
            workspace_id=workspace_id,
            gpu=["T4"],
            gpu_count=1,
        ) == ["T4"]
        assert (
            admission.admit_container_start(
                session,
                workspace_id=workspace_id,
                gpu=(),
                gpu_count=0,
            )
            == []
        )


def test_the_first_workspace_needs_no_account_and_the_second_needs_the_plan(
    isolated_services: ApiServices,
) -> None:
    """Sign-in creates a workspace before billing exists, so the first is free.

    Gated on terms, the workspace an account is given on its first sign-in would
    be refused for an account a few statements away from holding a subscription,
    and the sign-in meant to create both would leave neither.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    owner_user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    newcomer = isolated_services.users.create(display_name="no-account-yet")
    admission = DatabaseBillingAdmission()

    with isolated_services.context.database.session() as session:
        admission.assert_may_create_workspace(session, owner_user_id=newcomer.id)

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(CapacityLimitReachedError, match="already owns 1 workspaces"),
    ):
        admission.assert_may_create_workspace(session, owner_user_id=owner_user_id)


def test_a_plan_change_names_the_gpu_model_the_target_plan_does_not_offer(
    isolated_services: ApiServices,
) -> None:
    """Moving down while holding hardware the smaller plan does not sell.

    The model is what the customer has to act on, so it is what the refusal
    says: told only that they are over a limit, there is nothing for them to
    stop.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{user_id}",
            provider_subscription_id=f"sub_{user_id}",
            provider_credit_grant_id=f"credgr_{user_id}",
            plan=BillingPlanId.Team,
            subscription_terms_version=SubscriptionTermsVersion.Team,
            scheduled_terms_version=None,
            scheduled_change_at=None,
        )
        session.commit()
    _hold_gpu_cards(isolated_services, workspace_id=workspace_id, cards=1, model="H100")

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ConflictError, match="1 running containers on H100"),
    ):
        DatabaseBillingAdmission().assert_plan_change_fits(
            session,
            user_id=user_id,
            target=BillingPlanId.Free,
        )


def _carded_free_account(services: ApiServices) -> str:
    """The default workspace's owner, on the free plan with a card attached.

    A plan's own terms are only observable with a card: without one every
    account is given the cardless figures whatever it is subscribed to.
    """

    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    with services.context.database.session() as session:
        BillingAccountRepository(session).set_payment_method_present(
            user_id=user_id,
            present=True,
            at=utc_now(),
        )
        session.commit()
    return workspace_id


def _hold_gpu_cards(
    services: ApiServices,
    *,
    workspace_id: str,
    cards: int,
    model: str = "T4",
) -> None:
    with services.context.database.session() as session:
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=str(uuid4()),
                name=f"gpu-{uuid4().hex[:8]}",
                image="",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Running,
                gpu=[model],
                gpu_count=cards,
            )
        )
        session.commit()


def test_a_complimentary_account_is_admitted_on_team_terms_without_a_subscription(
    isolated_services: ApiServices,
) -> None:
    """A waived account runs on the Team plan's terms, and returns to its own when unwaived.

    The account here has never signed in, so it holds no subscription and would
    be refused every start. The waiver is what admits it, and what it is admitted
    to is the proof the terms are Team's rather than the free plan's: a card the
    free plan does not sell, and the two capabilities it does not include.
    Withdrawing the waiver has to refuse it again, since the alternative is an
    account that stays free for good the first time somebody grants and then
    reconsiders.
    """

    user_id, workspace_id = unbilled_account(isolated_services.context)
    admission = DatabaseBillingAdmission()
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="holds no subscription"),
    ):
        admission.admit_container_start(session, workspace_id=workspace_id, gpu=[], gpu_count=0)

    with isolated_services.context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.lock_for_registration(user_id)
        accounts.set_complimentary(user_id=user_id, present=True, at=utc_now())

    with isolated_services.context.database.session() as session:
        assert (
            admission.admit_container_start(session, workspace_id=workspace_id, gpu=[], gpu_count=0)
            == []
        )
        assert admission.admit_container_start(
            session, workspace_id=workspace_id, gpu=["H100"], gpu_count=1
        ) == ["H100"]
        admission.assert_may_take_on_billed_work(session, workspace_id=workspace_id)
        admission.assert_may_use_connected_cloud(session, user_id=user_id)
        admission.assert_may_use_custom_domains(session, user_id=user_id)
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(ConflictError, match="complimentary"),
    ):
        admission.assert_plan_change_fits(session, user_id=user_id, target=BillingPlanId.Team)

    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).set_complimentary(
            user_id=user_id, present=False, at=utc_now()
        )
    with (
        isolated_services.context.database.session() as session,
        pytest.raises(PaymentRequiredError, match="holds no subscription"),
    ):
        admission.admit_container_start(session, workspace_id=workspace_id, gpu=[], gpu_count=0)

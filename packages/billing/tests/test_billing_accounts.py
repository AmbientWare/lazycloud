from __future__ import annotations

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.billing_accounts import BillingAccountStatus, BillingPlan
from shared.identity import WorkspaceRole
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

from billing import BillingAccountService


def test_a_workspace_resolves_its_own_owners_account_and_nobody_elses(
    isolated_services: ApiServices,
) -> None:
    """Who pays is reached through the workspace's owner, and stops there.

    One account backs every workspace its owner holds, so the lookup joins on
    ownership rather than on the workspace. Two ways to get it wrong both charge
    the wrong card: dropping the workspace filter reaches any account at all, and
    dropping the owner filter reaches the account of anyone who merely belongs to
    the workspace — which is why the payer is given a non-owner membership of the
    stranger's workspace here.
    """

    control = ControlPlaneService(isolated_services.context)
    stranger = owned_workspace(control, "stranger")
    with isolated_services.context.database.session() as session:
        paying_workspace_id = isolated_services.context.default_workspace_id(session)
    paying_user_id = workspace_owner_user_id(isolated_services.context, paying_workspace_id)
    with isolated_services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=paying_user_id,
            plan=BillingPlan.Team,
            status=BillingAccountStatus.PastDue,
            provider_customer_id="cus_paying",
        )
        WorkspaceMemberRepository(session).add(
            workspace_id=stranger.id,
            user_id=paying_user_id,
            role=WorkspaceRole.Member,
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        accounts = BillingAccountService(session)
        paying = accounts.resolve_for_workspace(paying_workspace_id)
        borrowed = accounts.resolve_for_workspace(stranger.id)

    assert paying.plan is BillingPlan.Team
    assert paying.provider_customer_id == "cus_paying"
    assert paying.status is BillingAccountStatus.PastDue
    # The stranger's workspace has a payer among its members and still reads as
    # the free plan, because membership is not who pays.
    assert borrowed.plan is BillingPlan.Free
    assert borrowed.provider_customer_id == ""


def test_an_account_keeps_one_identity_across_repeated_payments(
    isolated_services: ApiServices,
) -> None:
    """The row's id is what later billing records point at, so writes must not move it.

    A first payment and an upgrade both arrive as "this user pays"; neither knows
    whether a row exists. If a write minted its own identity, a ledger entry or a
    period keyed on what a read returned would point at no account at all.
    """

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)

    with isolated_services.context.database.session() as session:
        repository = BillingAccountRepository(session)
        first = repository.upsert(
            user_id=user_id, plan=BillingPlan.Free, status=BillingAccountStatus.Active
        )
        upgraded = repository.upsert(
            user_id=user_id,
            plan=BillingPlan.Team,
            status=BillingAccountStatus.Active,
            provider_customer_id="cus_upgraded",
        )
        session.commit()

    with isolated_services.context.database.session() as session:
        stored = BillingAccountRepository(session).get_for_workspace_owner(workspace_id)

    assert stored is not None
    assert first.id == upgraded.id == stored.id
    assert stored.plan is BillingPlan.Team
    assert stored.provider_customer_id == "cus_upgraded"
    assert stored.created_at == first.created_at

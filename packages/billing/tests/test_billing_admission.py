from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.records.apps import AppRecord
from database.repositories.apps import AppRepository
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.custom_domains import CustomDomainRepository
from database.tables.orchestration import ContainerTable
from database.tables.storage import VolumeTable
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.custom_domains import CustomDomain
from shared.errors import CapacityLimitReachedError, ConflictError, PaymentRequiredError
from shared.http.volumes import GetOrCreateVolumeRequest
from shared.timestamps import utc_now
from sqlalchemy import func, select
from tests.service_fixtures import workspace_owner_user_id

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


def test_free_plan_refuses_an_app_beyond_its_account_limit(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        repository = AppRepository(session)
        for index in range(200):
            repository.upsert(
                AppRecord(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    name=f"quota-app-{index}",
                )
            )

    with pytest.raises(CapacityLimitReachedError, match="200 apps"):
        isolated_services.apps.create("one_more_app", workspace="default")


def test_free_plan_counts_distinct_members_across_owned_workspaces(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    admission = DatabaseBillingAdmission()
    first = isolated_services.users.create(display_name="member-one")
    second = isolated_services.users.create(display_name="member-two")
    refused = isolated_services.users.create(display_name="member-three")
    isolated_services.users.add_member(
        workspace_id=workspace_id,
        user_id=first.id,
        admission=admission,
    )
    isolated_services.users.add_member(
        workspace_id=workspace_id,
        user_id=second.id,
        admission=admission,
    )

    with pytest.raises(CapacityLimitReachedError, match="3 members"):
        isolated_services.users.add_member(
            workspace_id=workspace_id,
            user_id=refused.id,
            admission=admission,
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
    """The two halves of the only control a volume has, which pull opposite ways.

    Storage is the one billed thing that keeps costing with nothing running. The
    sweep that protects the money stops containers, so an account with a volume
    and nothing running is one it can do nothing about — and for a cardless
    account past its allowance, what accrues is not billed late but lost. Refusing
    the volume is the only point where that is preventable.

    Refusing to *resolve* one is a different act entirely: that is how a container
    mounts a volume and how its owner reads their own files back, and an account
    locked out of its data over a few cents of storage is a data-loss incident
    wearing a billing control's clothes. So creation is refused and resolution is
    not, and this is the seam where a later simplification would quietly merge
    them.
    """

    volumes = isolated_services.volume_service
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(isolated_services.context, workspace_id)
    existing = volumes.get_or_create_volume(
        GetOrCreateVolumeRequest(name="already-here"),
        workspace_id=workspace_id,
    )
    assert existing.volume is not None
    _exhaust_the_cardless_allowance(isolated_services, user_id)

    with pytest.raises(PaymentRequiredError, match="without a payment method"):
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


def _exhaust_the_cardless_allowance(services: ApiServices, user_id: str) -> None:
    """Leave the account nothing to spend, the way spending all of it does.

    Written as a period with no allowance rather than as usage against one: what
    admission reads is what remains, and a cycle that opened with nothing is the
    same answer arrived at without metering a container to get there.
    """

    now = utc_now()
    with services.context.database.session() as session:
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=now - timedelta(days=1),
            period_ended_at=now + timedelta(days=29),
            allowance_nanos=0,
            funded=False,
        )
        session.commit()


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

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.identity import WorkspaceMemberRepository
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.identity import TokenKind, WorkspaceRole
from shared.timestamps import utc_now
from shared.usage import (
    METERING_WINDOW_ENDED_AT_METADATA_KEY,
    METERING_WINDOW_STARTED_AT_METADATA_KEY,
    UsageMetric,
    UsageRecord,
    UsageUnit,
)
from tests.workspaces import owned_workspace, workspace_owner_user_id

_RATE_AT = timedelta(minutes=1)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=60)


def test_account_costs_sum_what_this_account_pays_for_and_nothing_else(
    unpriced_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """One figure for the account, over exactly the rows it is invoiced for.

    The provider invoices an account, so someone running dev, staging and prod
    holds several workspaces against one payment relationship and wants the
    total across them — reaching it a workspace at a time leaves them adding up
    their own bill.

    Which rows those are is decided by the payer on the ledger row, never by
    workspace membership. Somebody added to a colleague's workspace can watch
    everything in it and pays for none of it, so a total scoped by membership
    reads that colleague's spend into their bill — and disagrees with the
    allowance line on the same page and with the invoice, both of which are
    summed over the payer. A workspace nothing connects them to is absent under
    either rule, and stays here as the coarser half of the boundary.
    """

    now = utc_now()
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    control = ControlPlaneService(unpriced_services.context)
    with unpriced_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.account-costs",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        held = unpriced_services.context.default_workspace_id(session)
    owner_user_id = workspace_owner_user_id(unpriced_services.context, held)

    # A workspace somebody else pays for and this person was added to, and one
    # they cannot reach at all.
    colleague = owned_workspace(control, f"colleague-{uuid4().hex[:8]}")
    stranger = owned_workspace(control, f"stranger-{uuid4().hex[:8]}")
    with unpriced_services.context.database.session() as session:
        WorkspaceMemberRepository(session).add(
            workspace_id=colleague.id, user_id=owner_user_id, role=WorkspaceRole.Member
        )

    for workspace_id, quantity in ((held, 300), (colleague.id, 200), (stranger.id, 900)):
        unpriced_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="workspace",
                resource_id=workspace_id,
                metric=UsageMetric.NetworkEgressBytes,
                quantity=quantity,
                unit=UsageUnit.Bytes,
                labels={"app_id": str(uuid4()), "stub_id": str(uuid4())},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: ended_at.isoformat(),
                },
            )
        )

    issuer = TokenIssuer(unpriced_services.context)
    with unpriced_services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(
            session, "account-costs-owner", user_id=owner_user_id, kind=TokenKind.User
        )
    client = client_stack.enter_context(TestClient(create_app(unpriced_services)))

    response = client.get(
        "/api/v1/billing/costs",
        params={
            "start": (started_at - _WINDOW).isoformat(),
            "end": (ended_at + _WINDOW).isoformat(),
            "group_by": "app",
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cost_nanos"] == 300, (
        "the account total is not the sum of what this account is invoiced for: "
        f"{body['cost_nanos']} against 300"
    )
    assert body["workspace_id"] == "", "an account-wide page named one of its workspaces"
    assert [(row["workspace_id"], row["cost_nanos"]) for row in body["data"]] == [(held, 300)], (
        f"an account page's rows no longer name the workspace each cost arose in: {body['data']}"
    )


def test_account_cost_series_buckets_the_window_and_stops_at_the_payer(
    unpriced_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The shape of an account's spend, over exactly the intervals it was asked for.

    A chart is drawn from what comes back, so an interval nothing ran in has to
    arrive costing nothing rather than not arrive: a series that skips its quiet
    hours draws two spends an hour apart as two spends side by side, and the
    reader takes a flat week for a busy one.

    Scoped the way the total beside it is, and proven the same way: the spend of
    a workspace this person was added to but does not pay for is absent from
    every interval. The chart and the figure above it are read off one page, so
    a shape drawn over a wider set of rows than the total is a chart that does
    not add up to itself.
    """

    now = utc_now()
    # An hour boundary far enough ahead that the rate below is already effective
    # when the first metering window opens.
    origin = (now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    control = ControlPlaneService(unpriced_services.context)
    with unpriced_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.account-series",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        held = unpriced_services.context.default_workspace_id(session)
    owner_user_id = workspace_owner_user_id(unpriced_services.context, held)
    colleague = owned_workspace(control, f"colleague-{uuid4().hex[:8]}")
    with unpriced_services.context.database.session() as session:
        WorkspaceMemberRepository(session).add(
            workspace_id=colleague.id, user_id=owner_user_id, role=WorkspaceRole.Member
        )

    for workspace_id, hour, quantity in (
        (held, 0, 300),
        (held, 2, 500),
        (colleague.id, 1, 900),
    ):
        started_at = origin + timedelta(hours=hour)
        unpriced_services.usage.append(
            UsageRecord(
                id=str(uuid4()),
                workspace_id=workspace_id,
                resource_type="workspace",
                resource_id=workspace_id,
                metric=UsageMetric.NetworkEgressBytes,
                quantity=quantity,
                unit=UsageUnit.Bytes,
                labels={"app_id": str(uuid4()), "stub_id": str(uuid4())},
                metadata={
                    METERING_WINDOW_STARTED_AT_METADATA_KEY: started_at.isoformat(),
                    METERING_WINDOW_ENDED_AT_METADATA_KEY: (started_at + _WINDOW).isoformat(),
                },
            )
        )

    issuer = TokenIssuer(unpriced_services.context)
    with unpriced_services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(
            session, "account-series-owner", user_id=owner_user_id, kind=TokenKind.User
        )
    client = client_stack.enter_context(TestClient(create_app(unpriced_services)))

    response = client.get(
        "/api/v1/billing/cost-series",
        params={
            "start": origin.isoformat(),
            "end": (origin + timedelta(hours=4)).isoformat(),
            "bucket": "hour",
        },
        headers={"Authorization": f"Bearer {raw_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [bucket["cost_nanos"] for bucket in body["data"]] == [300, 0, 500, 0], (
        "the series is not one interval per hour of the window, holding only this "
        f"account's spend: {body['data']}"
    )
    assert body["cost_nanos"] == 800, (
        f"the total and the intervals it is drawn from disagree: {body['cost_nanos']}"
    )
    assert [datetime.fromisoformat(bucket["started_at"]) for bucket in body["data"]] == [
        origin + timedelta(hours=hour) for hour in range(4)
    ]
    assert [total["dimension"] for total in body["data"][0]["dimensions"]] == ["network_egress"]
    assert body["data"][1]["dimensions"] == [], (
        "an interval nothing was metered in reported a measurement"
    )

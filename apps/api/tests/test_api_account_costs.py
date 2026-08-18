from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
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
from tests.service_fixtures import owned_workspace, workspace_owner_user_id

_RATE_AT = timedelta(minutes=1)
_WINDOW_AT = timedelta(minutes=2)
_WINDOW = timedelta(seconds=60)


def test_account_costs_sum_the_caller_workspaces_and_nobody_else_s(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """One figure for the account, and only for the workspaces it is invoiced for.

    The provider invoices an account, so someone running dev, staging and prod
    holds several workspaces against one payment relationship and wants the
    total across them — reaching it a workspace at a time leaves them adding up
    their own bill.

    The same scope is the authorization boundary: this resolves the workspaces
    from the membership rows naming the caller rather than from anything the
    request supplied, so a third workspace's spend is absent here whatever the
    request asks for. A total that reached beyond membership would disclose one
    customer's spend to another.
    """

    now = utc_now()
    started_at = now + _WINDOW_AT
    ended_at = started_at + _WINDOW
    control = ControlPlaneService(isolated_services.context)
    with isolated_services.context.database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="test.account-costs",
            effective_at=now + _RATE_AT,
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )
        held = isolated_services.context.default_workspace_id(session)
    owner_user_id = workspace_owner_user_id(isolated_services.context, held)

    # A second workspace the same person owns, and a third they do not.
    second = owned_workspace(control, f"second-{uuid4().hex[:8]}")
    stranger = owned_workspace(control, f"stranger-{uuid4().hex[:8]}")
    with isolated_services.context.database.session() as session:
        WorkspaceMemberRepository(session).add(
            workspace_id=second.id, user_id=owner_user_id, role=WorkspaceRole.Member
        )

    for workspace_id, quantity in ((held, 300), (second.id, 200), (stranger.id, 900)):
        isolated_services.usage.append(
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

    issuer = TokenIssuer(isolated_services.context)
    with isolated_services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(
            session, "account-costs-owner", user_id=owner_user_id, kind=TokenKind.User
        )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

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
    assert body["cost_nanos"] == 500, (
        "the account total is not the sum of the workspaces this person holds: "
        f"{body['cost_nanos']} against 300 + 200"
    )
    assert body["workspace_id"] == "", "an account-wide page named one of its workspaces"

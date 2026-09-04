from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.email_outbox import EmailOutboxRepository
from database.repositories.identity import UserRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.identity import WorkspaceRole
from shared.timestamps import utc_now
from tests.service_fixtures import owned_workspace

from billing import DatabaseBillingAdmission

_INVITATIONS_PATH = "/invitations/"


def _queued_link(services: ApiServices) -> str:
    """The token the queued message carries. Nothing else ever holds it."""
    with services.context.database.session() as session:
        claimed = EmailOutboxRepository(session).claim(
            now=utc_now(), limit=10, claim_token="test-drain"
        )
    for item in claimed:
        for line in item.message.text.splitlines():
            if _INVITATIONS_PATH in line:
                return line.rsplit("/", 1)[1].strip()
    raise AssertionError("no invitation link was queued")


def _signed_in(services: ApiServices, name: str, email: str) -> tuple[str, dict[str, str]]:
    return _credential(services, services.users.create(display_name=name).id, name, email)


def _owner(services: ApiServices, workspace_id: str, email: str) -> tuple[str, dict[str, str]]:
    """The fixture's owner, on a plan with room for the people this test invites.

    The free plan is one seat and the owner holds it; that refusal is proven in
    the billing owner's admission tests rather than here.
    """
    owner = next(m for m in services.users.members(workspace_id) if m.role is WorkspaceRole.Owner)
    with services.context.database.session() as session:
        BillingAccountRepository(session).upsert(
            user_id=owner.user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_{owner.user_id}",
            provider_subscription_id=f"sub_{owner.user_id}",
            provider_credit_grant_id=f"credgr_{owner.user_id}",
            plan=BillingPlanId.Team,
        )
    return _credential(services, owner.user_id, "owner", email)


def _credential(
    services: ApiServices, user_id: str, name: str, email: str
) -> tuple[str, dict[str, str]]:
    with services.context.database.session() as session:
        UserRepository(session).set_profile(user_id, display_name=name, email=email, avatar_url="")
    raw, _record = AuthService(services.context).create_account_token(user_id, f"{name}-cli")
    return user_id, {"Authorization": f"Bearer {raw}"}


def test_invitations_are_an_administrators_to_send_read_and_answered_by_their_addressee(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The authorization each route applies, which only the routes decide.

    Sending an invitation and reading who has one are both administrators' acts:
    the pending list names people with no membership here, so a member who could
    read it would learn who the workspace is recruiting. Leaving needs no
    administrator, and removing somebody else still does.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    member_id, member = _signed_in(isolated_services, "member", "member@example.test")
    isolated_services.users.add_member(
        workspace_id=workspace.id,
        user_id=member_id,
        admission=DatabaseBillingAdmission(),
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    refused = client.post(
        "/api/v1/workspaces/team/invitations",
        json={"email": "new@example.test", "role": "member"},
        headers=member,
    )
    assert refused.status_code == 403, refused.text

    created = client.post(
        "/api/v1/workspaces/team/invitations",
        json={"email": "New@Example.test", "role": "administrator"},
        headers=owner,
    )
    assert created.status_code == 201, created.text

    assert client.get("/api/v1/workspaces/team/invitations", headers=member).status_code == 403
    listed = client.get("/api/v1/workspaces/team/invitations", headers=owner)
    assert [item["id"] for item in listed.json()["data"]] == [created.json()["id"]]

    # Deliberately an address unlike the one invited: the link is what joins.
    invited_id, invited = _signed_in(isolated_services, "new", "elsewhere@other.test")
    token = _queued_link(isolated_services)
    accepted = client.post(f"/api/v1/invitations/{token}/accept", headers=invited)
    assert accepted.status_code == 201, accepted.text
    assert client.post(f"/api/v1/invitations/{token}/accept", headers=invited).status_code == 404

    kept = client.delete(f"/api/v1/workspaces/team/members/{invited_id}", headers=member)
    assert kept.status_code == 403, kept.text
    left = client.delete(f"/api/v1/workspaces/team/members/{member_id}", headers=member)
    assert left.status_code == 204, left.text
    assert client.get("/api/v1/workspaces/team/members", headers=member).status_code == 403
    assert {owner_id, invited_id} == {
        item["user_id"]
        for item in client.get("/api/v1/workspaces/team/members", headers=owner).json()["data"]
    }


def test_an_unknown_link_and_a_malformed_id_are_refused_rather_than_faulting(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A link that opens nothing is a 404, and a bad id is a 400.

    The invitation id reaches a uuid column, and unvalidated it raises inside the
    driver, which nothing maps, so the caller gets a 500 for their own typo. A
    token is opaque and simply matches nothing.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    assert client.get("/api/v1/invitations/nothing-here", headers=owner).status_code == 404
    assert client.post("/api/v1/invitations/nothing-here/accept", headers=owner).status_code == 404
    assert (
        client.delete("/api/v1/workspaces/team/invitations/not-a-uuid", headers=owner).status_code
        == 400
    )

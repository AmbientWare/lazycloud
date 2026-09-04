from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.billing import BillingAccountRepository
from database.repositories.identity import UserRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.email import EmailMessage
from shared.identity import WorkspaceRole
from tests.service_fixtures import owned_workspace

from billing import DatabaseBillingAdmission


@dataclass(slots=True)
class _Outbox:
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


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
    isolated_services.invitations.mailer = lambda: _Outbox()
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

    invited_id, invited = _signed_in(isolated_services, "new", "new@example.test")
    assert client.get("/api/v1/invitations", headers=member).json()["data"] == []
    accepted = client.post(f"/api/v1/invitations/{created.json()['id']}/accept", headers=invited)
    assert accepted.status_code == 201, accepted.text

    kept = client.delete(f"/api/v1/workspaces/team/members/{invited_id}", headers=member)
    assert kept.status_code == 403, kept.text
    left = client.delete(f"/api/v1/workspaces/team/members/{member_id}", headers=member)
    assert left.status_code == 204, left.text
    assert client.get("/api/v1/workspaces/team/members", headers=member).status_code == 403
    assert {owner_id, invited_id} == {
        item["user_id"]
        for item in client.get("/api/v1/workspaces/team/members", headers=owner).json()["data"]
    }


def test_a_malformed_invitation_id_is_refused_rather_than_reaching_the_database(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """A path segment that is not a uuid is a bad request, not a server fault.

    Unvalidated it reaches a uuid column and raises inside the driver, which
    nothing maps, so the caller gets a 500 for their own typo.
    """
    workspace = owned_workspace(isolated_services.control_plane_service, "team")
    _owner_id, owner = _owner(isolated_services, workspace.id, "owner@example.test")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    assert client.post("/api/v1/invitations/not-a-uuid/accept", headers=owner).status_code == 400
    assert (
        client.delete("/api/v1/workspaces/team/invitations/not-a-uuid", headers=owner).status_code
        == 400
    )

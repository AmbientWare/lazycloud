from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.identity import UserRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
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
    """The fixture's owner, who is the account the workspace bills through."""
    owner = next(m for m in services.users.members(workspace_id) if m.role is WorkspaceRole.Owner)
    return _credential(services, owner.user_id, "owner", email)


def _credential(
    services: ApiServices, user_id: str, name: str, email: str
) -> tuple[str, dict[str, str]]:
    with services.context.database.session() as session:
        UserRepository(session).set_profile(user_id, display_name=name, email=email, avatar_url="")
    raw, _record = AuthService(services.context).create_account_token(user_id, f"{name}-cli")
    return user_id, {"Authorization": f"Bearer {raw}"}


def test_invitations_are_an_administrators_to_send_and_the_invitees_to_answer(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The routes hold the same line the service does, from the outside.

    A member cannot invite; an administrator can. The person invited answers under
    their own credential and arrives as a member. A member may take themself out
    but nobody else, so leaving needs no administrator and removal still does.
    """
    outbox = _Outbox()
    isolated_services.invitations.mailer = lambda: outbox
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
    assert created.json()["email"] == "new@example.test"
    assert [message.to for message in outbox.sent] == ["new@example.test"]

    listed = client.get("/api/v1/workspaces/team/invitations", headers=owner)
    assert [item["id"] for item in listed.json()["data"]] == [created.json()["id"]]

    invited_id, invited = _signed_in(isolated_services, "new", "new@example.test")
    pending = client.get("/api/v1/invitations", headers=invited)
    assert pending.status_code == 200, pending.text
    assert [item["workspace_name"] for item in pending.json()["data"]] == ["team"]
    assert client.get("/api/v1/invitations", headers=member).json()["data"] == []

    accepted = client.post(f"/api/v1/invitations/{created.json()['id']}/accept", headers=invited)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["role"] == "administrator"
    members = client.get("/api/v1/workspaces/team/members", headers=invited)
    assert {item["user_id"] for item in members.json()["data"]} == {owner_id, member_id, invited_id}

    kept = client.delete(f"/api/v1/workspaces/team/members/{invited_id}", headers=member)
    assert kept.status_code == 403, kept.text
    left = client.delete(f"/api/v1/workspaces/team/members/{member_id}", headers=member)
    assert left.status_code == 204, left.text
    assert client.get("/api/v1/workspaces/team/members", headers=member).status_code == 403

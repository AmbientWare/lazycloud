from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from api.server.services import ApiServices
from database.repositories.identity import UserIdentityRepository, WorkspaceMemberRepository
from identity.sign_in import SignInService
from shared.errors import UpstreamUnavailableError
from shared.external_identity import ExternalIdentityProfile
from shared.identity import IdentityProvider

_SUBJECT = "4815162342"
_LOGIN = "octo"


@dataclass(slots=True)
class _RecordingIdentityProvider:
    """The provider half of the flow, answering with one fixed person."""

    states: list[str] = field(default_factory=list)

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        del code_challenge
        self.states.append(state)
        return f"https://identity.invalid/authorize?state={state}"

    def identify(self, *, code: str, code_verifier: str) -> ExternalIdentityProfile:
        del code, code_verifier
        return ExternalIdentityProfile(
            provider=IdentityProvider.Github,
            subject=_SUBJECT,
            login=_LOGIN,
            display_name="Octo Cat",
            email="octo@example.invalid",
        )


@dataclass(slots=True)
class _FailingThenRecordingProvisioner:
    """Provisioning that is unavailable once, the way an outage arrives."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_next: bool = True

    def __call__(self, *, user_id: str, workspace_id: str) -> None:
        self.calls.append((user_id, workspace_id))
        if self.fail_next:
            self.fail_next = False
            raise UpstreamUnavailableError("the payment provider is unreachable")


def test_a_sign_in_whose_provisioning_fails_mints_no_session(
    isolated_services: ApiServices,
) -> None:
    """A refused provisioning leaves an account that cannot be signed in as.

    The account and its workspace survive the failure — they are what the retry
    is idempotent against — but no exchange code is minted, so there is nothing
    to redeem and no session exists. That ordering is the whole guarantee: an
    account the platform could not finish provisioning can never start work,
    because nothing it could authenticate with was ever handed out.
    """

    identity = _RecordingIdentityProvider()
    provisioner = _FailingThenRecordingProvisioner()
    service = SignInService(
        context=isolated_services.context,
        redis=isolated_services.sign_in.redis,
        provider_factory=lambda: identity,
        provision_default_workspace=isolated_services.control_plane_service.ensure_default_workspace,
        provision_billing_account=provisioner,
    )

    service.start()
    with pytest.raises(UpstreamUnavailableError):
        service.complete(code="auth-code", state=identity.states[-1])

    with isolated_services.context.database.session() as session:
        linked = UserIdentityRepository(session).by_subject(
            provider=IdentityProvider.Github,
            subject=_SUBJECT,
        )
        assert linked is not None
        owned = WorkspaceMemberRepository(session).owned_workspace_ids(linked.user_id)
    assert owned

    retried = service.start()
    exchange_code = service.complete(code="auth-code", state=identity.states[-1])
    session_credential = service.redeem(code=exchange_code, nonce=retried.nonce)

    assert session_credential.user.id == linked.user_id
    assert [call[0] for call in provisioner.calls] == [linked.user_id, linked.user_id]
    # One workspace across both attempts, and one the account already owns: a
    # retry that provisioned against a second would bill the wrong thing.
    assert {call[1] for call in provisioner.calls} == {owned[0]}

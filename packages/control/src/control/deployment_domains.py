from __future__ import annotations

from typing import Protocol

from database.repositories.custom_domains import CustomDomainRepository
from database.repositories.identity import WorkspaceMemberRepository
from shared.errors import InvalidInputError
from sqlalchemy.orm import Session


class CustomDomainUseAdmission(Protocol):
    def assert_may_use_custom_domains(
        self,
        session: Session,
        *,
        user_id: str,
    ) -> None: ...


def claimed_hostname(
    session: Session,
    domain: str | None,
    *,
    workspace_id: str,
    admission: CustomDomainUseAdmission,
) -> str | None:
    """Resolve the hostname a spec claims, refusing one the deployer cannot serve.

    Checked against the registrations held by the account that owns this workspace,
    so a spec cannot claim a name under a domain another tenant proved it owns, and a
    domain registered once serves deployments in any workspace that account owns. A
    registration still short of `ready` is accepted: the certificate arrives on the
    provider's schedule, and a deploy that failed until it did would make an ordinary
    redeploy depend on DNS propagation.
    """

    if domain is None:
        return None
    owner = WorkspaceMemberRepository(session).owner(workspace_id)
    if owner is not None:
        admission.assert_may_use_custom_domains(session, user_id=owner.user_id)
    registered = (
        CustomDomainRepository(session).get_by_hostname(domain, user_id=owner.user_id)
        if owner is not None
        else None
    )
    if registered is None:
        raise InvalidInputError(
            f"{domain} is not registered to this account; "
            f"register it before a deployment can serve it"
        )
    return domain

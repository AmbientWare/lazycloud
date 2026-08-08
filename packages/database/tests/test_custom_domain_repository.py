from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.custom_domains import CustomDomainRepository
from shared.custom_domains import CustomDomain
from shared.errors import ConflictError


def _register(services: ApiServices, *, workspace_id: str, hostname: str) -> None:
    with services.context.database.session() as session:
        CustomDomainRepository(session).create(
            CustomDomain(id=str(uuid4()), workspace_id=workspace_id, hostname=hostname),
            workspace_id=workspace_id,
        )


def test_one_workspaces_registration_never_covers_anothers_hostname(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = control.upsert_workspace("domain-owner")
    intruder = control.upsert_workspace("domain-intruder")
    _register(isolated_services, workspace_id=owner.id, hostname="*.acme.com")

    with isolated_services.context.database.session() as session:
        repository = CustomDomainRepository(session)

        assert repository.covering("api.acme.com", workspace_id=owner.id) is not None
        assert repository.covering("api.acme.com", workspace_id=intruder.id) is None


def test_a_hostname_can_be_registered_by_only_one_workspace(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owner = control.upsert_workspace("first-claimant")
    rival = control.upsert_workspace("second-claimant")
    _register(isolated_services, workspace_id=owner.id, hostname="acme.com")

    with pytest.raises(ConflictError):
        _register(isolated_services, workspace_id=rival.id, hostname="acme.com")


@pytest.mark.parametrize(
    ("registered", "requested", "covered"),
    [
        ("*.acme.com", "api.acme.com", True),
        # A wildcard certificate secures one label, so the registration must not
        # promise more than the certificate can deliver.
        ("*.acme.com", "api.staging.acme.com", False),
        ("*.acme.com", "acme.com", False),
        ("*.acme.com", "notacme.com", False),
        ("acme.com", "acme.com", True),
        ("acme.com", "api.acme.com", False),
    ],
)
def test_a_registration_covers_exactly_what_its_certificate_would(
    isolated_services: ApiServices,
    registered: str,
    requested: str,
    covered: bool,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("coverage")
    _register(isolated_services, workspace_id=workspace.id, hostname=registered)

    with isolated_services.context.database.session() as session:
        found = CustomDomainRepository(session).covering(requested, workspace_id=workspace.id)

    assert (found is not None) is covered

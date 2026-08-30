from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.custom_domains import CustomDomainRepository
from identity.users import UserService
from shared.custom_domains import CustomDomain, CustomDomainDnsMode
from shared.errors import ConflictError


def _account(services: ApiServices, name: str) -> str:
    return UserService(services.context).create(display_name=name).id


def _register(
    services: ApiServices,
    *,
    user_id: str,
    hostname: str,
    dns_mode: CustomDomainDnsMode = CustomDomainDnsMode.Cname,
) -> None:
    with services.context.database.session() as session:
        CustomDomainRepository(session).create(
            CustomDomain(
                id=str(uuid4()),
                user_id=user_id,
                hostname=hostname,
                dns_mode=dns_mode,
            ),
            user_id=user_id,
        )


def test_one_accounts_registration_never_covers_anothers_hostname(
    isolated_services: ApiServices,
) -> None:
    owner = _account(isolated_services, "domain-owner")
    intruder = _account(isolated_services, "domain-intruder")
    _register(
        isolated_services,
        user_id=owner,
        hostname="acme.com",
        dns_mode=CustomDomainDnsMode.Delegation,
    )

    with isolated_services.context.database.session() as session:
        repository = CustomDomainRepository(session)

        assert repository.covering("api.acme.com", user_id=owner) is not None
        assert repository.covering("api.acme.com", user_id=intruder) is None


def test_a_hostname_can_be_registered_by_only_one_account(
    isolated_services: ApiServices,
) -> None:
    owner = _account(isolated_services, "first-claimant")
    rival = _account(isolated_services, "second-claimant")
    _register(isolated_services, user_id=owner, hostname="acme.com")

    with pytest.raises(ConflictError):
        _register(isolated_services, user_id=rival, hostname="acme.com")


@pytest.mark.parametrize(
    ("registered", "dns_mode", "requested", "covered"),
    [
        ("acme.com", CustomDomainDnsMode.Delegation, "api.acme.com", True),
        ("acme.com", CustomDomainDnsMode.Delegation, "api.staging.acme.com", False),
        ("acme.com", CustomDomainDnsMode.Delegation, "acme.com", True),
        ("acme.com", CustomDomainDnsMode.Delegation, "notacme.com", False),
        ("api.acme.com", CustomDomainDnsMode.Cname, "api.acme.com", True),
        ("api.acme.com", CustomDomainDnsMode.Cname, "other.acme.com", False),
    ],
)
def test_a_registration_covers_exactly_what_its_certificate_would(
    isolated_services: ApiServices,
    registered: str,
    dns_mode: CustomDomainDnsMode,
    requested: str,
    covered: bool,
) -> None:
    owner = _account(isolated_services, "coverage")
    _register(
        isolated_services,
        user_id=owner,
        hostname=registered,
        dns_mode=dns_mode,
    )

    with isolated_services.context.database.session() as session:
        found = CustomDomainRepository(session).covering(requested, user_id=owner)

    assert (found is not None) is covered

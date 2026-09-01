from __future__ import annotations

from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.custom_domains import CustomDomainRepository
from identity.users import UserService
from shared.custom_domains import CustomDomain
from shared.errors import ConflictError


def _account(services: ApiServices, name: str) -> str:
    return UserService(services.context).create(display_name=name).id


def _register(services: ApiServices, *, user_id: str, hostname: str) -> None:
    with services.context.database.session() as session:
        CustomDomainRepository(session).create(
            CustomDomain(id=str(uuid4()), user_id=user_id, hostname=hostname),
            user_id=user_id,
        )


def test_one_accounts_registration_never_satisfies_another_account(
    isolated_services: ApiServices,
) -> None:
    owner = _account(isolated_services, "domain-owner")
    intruder = _account(isolated_services, "domain-intruder")
    _register(isolated_services, user_id=owner, hostname="api.acme.com")

    with isolated_services.context.database.session() as session:
        repository = CustomDomainRepository(session)

        assert repository.get_by_hostname("api.acme.com", user_id=owner) is not None
        assert repository.get_by_hostname("api.acme.com", user_id=intruder) is None


def test_a_hostname_can_be_registered_by_only_one_account(
    isolated_services: ApiServices,
) -> None:
    owner = _account(isolated_services, "first-claimant")
    rival = _account(isolated_services, "second-claimant")
    _register(isolated_services, user_id=owner, hostname="acme.com")

    with pytest.raises(ConflictError):
        _register(isolated_services, user_id=rival, hostname="acme.com")

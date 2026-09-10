from __future__ import annotations

from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.repositories.custom_domains import CustomDomainRepository
from identity.users import UserService
from shared.custom_domains import CustomDomain
from shared.errors import ConflictError


def _account(context: ServiceContext, name: str) -> str:
    return UserService(context).create(display_name=name).id


def _register(context: ServiceContext, *, user_id: str, hostname: str) -> None:
    with context.database.session() as session:
        CustomDomainRepository(session).create(
            CustomDomain(id=str(uuid4()), user_id=user_id, hostname=hostname),
            user_id=user_id,
        )


def test_one_accounts_registration_never_satisfies_another_account(
    service_context: ServiceContext,
) -> None:
    owner = _account(service_context, "domain-owner")
    intruder = _account(service_context, "domain-intruder")
    _register(service_context, user_id=owner, hostname="api.acme.com")

    with service_context.database.session() as session:
        repository = CustomDomainRepository(session)

        assert repository.get_by_hostname("api.acme.com", user_id=owner) is not None
        assert repository.get_by_hostname("api.acme.com", user_id=intruder) is None


def test_a_hostname_can_be_registered_by_only_one_account(
    service_context: ServiceContext,
) -> None:
    owner = _account(service_context, "first-claimant")
    rival = _account(service_context, "second-claimant")
    _register(service_context, user_id=owner, hostname="acme.com")

    with pytest.raises(ConflictError):
        _register(service_context, user_id=rival, hostname="acme.com")

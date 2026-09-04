from __future__ import annotations

import pytest
from api.server.services import ApiServices
from identity.users import UserService
from shared.errors import ConflictError
from shared.identity import PlatformRole, UserStatus


def test_the_last_active_administrator_cannot_be_demoted_or_disabled(
    isolated_services: ApiServices,
) -> None:
    """Nothing an administrator does from the dashboard may lock everyone out.

    Demoting and disabling are the same lockout by two routes, because a
    disabled administrator authorizes as a member. Both are refused while the
    account is the only administrator and allowed once a second one exists, and
    the second can then be removed the same way only while the first remains.
    """

    users = UserService(isolated_services.context)
    first = users.create(display_name="first", role=PlatformRole.Administrator)

    with pytest.raises(ConflictError, match="last active administrator"):
        users.set_role(first.id, role=PlatformRole.Member)
    with pytest.raises(ConflictError, match="last active administrator"):
        users.set_status(first.id, status=UserStatus.Disabled)
    assert users.get(first.id).role is PlatformRole.Administrator
    assert users.get(first.id).status is UserStatus.Active

    second = users.create(display_name="second", role=PlatformRole.Administrator)
    assert users.set_status(first.id, status=UserStatus.Disabled).status is UserStatus.Disabled

    with pytest.raises(ConflictError, match="last active administrator"):
        users.set_role(second.id, role=PlatformRole.Member)
    assert users.set_status(first.id, status=UserStatus.Active).status is UserStatus.Active
    assert users.set_role(second.id, role=PlatformRole.Member).role is PlatformRole.Member

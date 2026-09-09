from __future__ import annotations

from datetime import timedelta

import pytest
from database.context import ServiceContext
from database.repositories.identity import (
    DeviceAuthorizationRepository,
)
from database.tables.identity import DeviceAuthorizationTable
from identity.users import UserService
from shared.identity import DeviceAuthorizationStatus
from shared.timestamps import utc_now
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


def test_device_authorization_unique_collision_preserves_transaction(
    service_context: ServiceContext,
) -> None:
    expires_at = utc_now() + timedelta(minutes=15)
    with service_context.database.session() as session:
        repository = DeviceAuthorizationRepository(session)
        first = repository.create_pending(
            device_code_hash="first-device-hash",
            user_code="BCDF-GHJK",
            client_name="first-client",
            expires_at=expires_at,
        )
        collision = repository.create_pending(
            device_code_hash="second-device-hash",
            user_code="BCDF-GHJK",
            client_name="collision-client",
            expires_at=expires_at,
        )
        second = repository.create_pending(
            device_code_hash="third-device-hash",
            user_code="LMNP-QRST",
            client_name="second-client",
            expires_at=expires_at,
        )

    assert first is not None
    assert collision is None
    assert second is not None


@pytest.mark.parametrize(
    ("status", "user", "consumed"),
    (
        (DeviceAuthorizationStatus.Pending, "user", False),
        (DeviceAuthorizationStatus.Pending, None, True),
        (DeviceAuthorizationStatus.Approved, None, False),
        (DeviceAuthorizationStatus.Denied, "user", False),
        (DeviceAuthorizationStatus.Expired, None, False),
    ),
)
def test_device_authorization_constraints_reject_invalid_state(
    service_context: ServiceContext,
    status: DeviceAuthorizationStatus,
    user: str | None,
    consumed: bool,
) -> None:
    approver = UserService(service_context).create(
        display_name="device-invariant-owner",
    )
    expires_at = utc_now() + timedelta(minutes=15)
    with service_context.database.session() as session:
        created = DeviceAuthorizationRepository(session).create_pending(
            device_code_hash=f"device-invariant-{status.value}-{user}-{consumed}",
            user_code="BCDF-GHJK",
            client_name="invariant-client",
            expires_at=expires_at,
        )
        assert created is not None

    statement = (
        update(DeviceAuthorizationTable)
        .where(DeviceAuthorizationTable.id == created.id)
        .values(
            status=status.value,
            user_id=approver.id if user is not None else None,
        )
    )
    if consumed:
        statement = statement.values(consumed_at=utc_now())
    with pytest.raises(IntegrityError), service_context.database.session() as session:
        session.execute(statement)

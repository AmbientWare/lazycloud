from __future__ import annotations

from datetime import timedelta

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import (
    DeviceAuthorizationRepository,
)
from database.tables.identity import DeviceAuthorizationTable
from shared.identity import DeviceAuthorizationStatus
from shared.timestamps import utc_now
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


def test_device_authorization_unique_collision_preserves_transaction(
    isolated_services: ApiServices,
) -> None:
    expires_at = utc_now() + timedelta(minutes=15)
    with isolated_services.context.database.session() as session:
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
    ("status", "workspace", "consumed"),
    (
        (DeviceAuthorizationStatus.Pending, "workspace", False),
        (DeviceAuthorizationStatus.Pending, None, True),
        (DeviceAuthorizationStatus.Approved, None, False),
        (DeviceAuthorizationStatus.Denied, "workspace", False),
        (DeviceAuthorizationStatus.Expired, None, False),
    ),
)
def test_device_authorization_constraints_reject_invalid_state(
    isolated_services: ApiServices,
    status: DeviceAuthorizationStatus,
    workspace: str | None,
    consumed: bool,
) -> None:
    owned_workspace = ControlPlaneService(isolated_services.context).upsert_workspace(
        "device-invariant-owner"
    )
    expires_at = utc_now() + timedelta(minutes=15)
    with isolated_services.context.database.session() as session:
        created = DeviceAuthorizationRepository(session).create_pending(
            device_code_hash=f"device-invariant-{status.value}-{workspace}-{consumed}",
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
            workspace_id=owned_workspace.id if workspace is not None else None,
        )
    )
    if consumed:
        statement = statement.values(consumed_at=utc_now())
    with pytest.raises(IntegrityError), isolated_services.context.database.session() as session:
        session.execute(statement)

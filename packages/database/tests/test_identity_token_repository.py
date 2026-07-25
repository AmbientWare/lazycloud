from __future__ import annotations

from datetime import UTC, datetime

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.identity import TokenRepository
from database.tables.identity import TokenTable
from shared.identity import TokenKind, TokenStatus
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError


@pytest.mark.parametrize(
    ("status", "revoked_at"),
    (
        (TokenStatus.Active, datetime(2026, 7, 21, 12, tzinfo=UTC)),
        (TokenStatus.Revoked, None),
    ),
)
def test_consumed_token_requires_terminal_revocation(
    isolated_services: ApiServices,
    status: TokenStatus,
    revoked_at: datetime | None,
) -> None:
    workspace = ControlPlaneService(isolated_services.context).upsert_workspace(
        "consumed-token-invariant"
    )
    consumed_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    with isolated_services.context.database.session() as session:
        created = TokenRepository(session).create(
            name="invalid-consumed-token",
            token_hash="pbkdf2_sha256$consumed$digest",
            prefix="rt_consume",
            kind=TokenKind.Workspace,
            workspace_id=workspace.id,
            reusable=False,
        )

    with pytest.raises(IntegrityError), isolated_services.context.database.session() as session:
        session.execute(
            update(TokenTable)
            .where(TokenTable.id == created.id)
            .values(
                status=status.value,
                consumed_at=consumed_at,
                revoked_at=revoked_at,
            )
        )

    with isolated_services.context.database.session() as session:
        retained = session.get(TokenTable, created.id)
        assert retained is not None
        assert retained.status == TokenStatus.Active.value
        assert retained.consumed_at is None
        assert retained.revoked_at is None

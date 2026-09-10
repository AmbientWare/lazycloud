from __future__ import annotations

from datetime import timedelta
from typing import Never

import pytest
from database.context import ServiceContext
from database.repositories.identity import DeviceAuthorizationRepository
from database.tables.identity import DeviceAuthorizationTable
from database.types import DatabaseSession
from identity.auth import TokenIssuer
from identity.device_auth import (
    DeviceAuthorizationService,
)
from identity.users import UserService
from shared.identity import (
    DeviceAuthorizationStatus,
    TokenKind,
)
from shared.timestamps import utc_now
from sqlalchemy import update


def test_expired_device_code_claim_reports_expired_before_prune(
    service_context: ServiceContext,
) -> None:
    service = DeviceAuthorizationService(service_context)
    started = service.start(client_name="cli")

    with service_context.database.session() as session:
        repository = DeviceAuthorizationRepository(session)
        record = repository.by_user_code(started.record.user_code)
        assert record is not None
        session.execute(
            update(DeviceAuthorizationTable)
            .where(DeviceAuthorizationTable.id == record.id)
            .values(expires_at=utc_now() - timedelta(seconds=1))
        )

    claim = service.claim(started.device_code)
    assert claim.status is DeviceAuthorizationStatus.Expired
    assert claim.token == ""


def test_device_claim_rolls_back_consumption_when_token_insert_fails(
    service_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DeviceAuthorizationService(service_context)
    started = service.start(client_name="cli")
    user = UserService(service_context).create(display_name="device-owner")
    service.approve(started.record.user_code, user_id=user.id)

    original_issue = TokenIssuer.issue_for_user

    def fail_issue(
        self: TokenIssuer,
        session: DatabaseSession,
        name: str,
        *,
        user_id: str,
        kind: TokenKind = TokenKind.User,
        scopes: list[str] | None = None,
        expires_in_seconds: int | None = None,
        reusable: bool = True,
    ) -> Never:
        del (self, session, name, user_id, kind, scopes, expires_in_seconds, reusable)
        raise RuntimeError("token insert failed")

    monkeypatch.setattr(TokenIssuer, "issue_for_user", fail_issue)
    with pytest.raises(RuntimeError, match="token insert failed"):
        service.claim(started.device_code)

    monkeypatch.setattr(TokenIssuer, "issue_for_user", original_issue)
    claimed = service.claim(started.device_code)
    assert claimed.status is DeviceAuthorizationStatus.Approved
    assert claimed.token

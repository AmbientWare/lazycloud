from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisSettings, RedisWireScalar, redis_text
from database.tables.identity import TokenTable
from identity.auth import AuthError, AuthorizationDeniedError, AuthService
from identity.websocket_tickets import (
    WEBSOCKET_TICKET_TTL_SECONDS,
    ShellWebSocketAudience,
    WebSocketTicketService,
    WebSocketTicketStoreError,
)
from shared.identity import AuthScope
from tests.redis_fakes import FakeRedis
from tests.service_fixtures import owned_workspace


def _ticket_service(isolated_services: ApiServices) -> tuple[WebSocketTicketService, FakeRedis]:
    fake = FakeRedis()
    redis = RedisClient(fake, key_prefix="ticket-test")
    return WebSocketTicketService(isolated_services.context, redis), fake


def _audience(workspace_id: str) -> ShellWebSocketAudience:
    return ShellWebSocketAudience(
        workspace_id=workspace_id,
        stub_id="stub-1",
        container_id="container-1",
    )


def test_ticket_is_hashed_short_lived_and_contains_no_bearer(
    isolated_services: ApiServices,
) -> None:
    raw_token, token = AuthService(isolated_services.context).create_token(
        "browser-shell",
        scopes=[AuthScope.Read.value],
    )
    service, fake = _ticket_service(isolated_services)

    ticket = service.mint_shell_ticket(token, audience=_audience(token.workspace_id))

    assert ticket.startswith("wst_")
    assert len(fake.values) == 1
    key, payload = next(iter(fake.values.items()))
    payload_text = redis_text(payload)
    assert ticket not in key
    assert raw_token not in payload_text
    assert "token_hash" not in payload_text
    assert fake.ttl(key) == WEBSOCKET_TICKET_TTL_SECONDS


def test_ticket_consumption_is_single_use_and_audience_bound(
    isolated_services: ApiServices,
) -> None:
    _raw_token, token = AuthService(isolated_services.context).create_token(
        "browser-shell",
        scopes=[AuthScope.Read.value],
    )
    service, _fake = _ticket_service(isolated_services)
    audience = _audience(token.workspace_id)
    wrong_ticket = service.mint_shell_ticket(token, audience=audience)

    with pytest.raises(AuthError, match="audience"):
        service.consume_shell_ticket(
            wrong_ticket,
            stub_id=audience.stub_id,
            container_id="another-container",
        )
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            wrong_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )

    ticket = service.mint_shell_ticket(token, audience=audience)
    authorization = service.consume_shell_ticket(
        ticket,
        stub_id=audience.stub_id,
        container_id=audience.container_id,
    )
    assert authorization.token.id == token.id
    assert authorization.audience == audience
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )


def test_failed_scope_and_revocation_checks_consume_ticket(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    _raw_token, token = auth.create_token(
        "browser-shell",
        scopes=[AuthScope.Read.value],
    )
    service, _fake = _ticket_service(isolated_services)
    audience = _audience(token.workspace_id)
    scope_ticket = service.mint_shell_ticket(token, audience=audience)
    with isolated_services.context.database.session() as session:
        current = session.get(TokenTable, token.id)
        assert current is not None
        current.scopes = [AuthScope.Write.value]

    with pytest.raises(AuthorizationDeniedError, match="missing scope"):
        service.consume_shell_ticket(
            scope_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            scope_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )

    with isolated_services.context.database.session() as session:
        current = session.get(TokenTable, token.id)
        assert current is not None
        current.scopes = [AuthScope.Read.value]
    revoked_ticket = service.mint_shell_ticket(token, audience=audience)
    auth.revoke_token(token.id)

    with pytest.raises(AuthError, match="invalid token identity"):
        service.consume_shell_ticket(
            revoked_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            revoked_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )


def test_expired_token_and_ticket_scope_mismatch_are_terminal(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    _raw_token, token = auth.create_token(
        "expiring-shell",
        scopes=[AuthScope.Read.value],
        expires_in_seconds=0,
    )
    service, _fake = _ticket_service(isolated_services)
    audience = _audience(token.workspace_id)
    expired_token_ticket = service.mint_shell_ticket(token, audience=audience)

    with pytest.raises(AuthError, match="expired"):
        service.consume_shell_ticket(
            expired_token_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            expired_token_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )

    _raw_active, active = auth.create_token(
        "scope-shell",
        scopes=[AuthScope.Read.value, AuthScope.Write.value],
    )
    scope_ticket = service.mint_shell_ticket(active, audience=_audience(active.workspace_id))
    with pytest.raises(AuthError, match="scope"):
        service.consume_shell_ticket(
            scope_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
            required_scope=AuthScope.Write,
        )
    with pytest.raises(AuthError, match="invalid or expired"):
        service.consume_shell_ticket(
            scope_ticket,
            stub_id=audience.stub_id,
            container_id=audience.container_id,
        )


def test_ticket_mint_rejects_wrong_workspace_and_insufficient_scope(
    isolated_services: ApiServices,
) -> None:
    auth = AuthService(isolated_services.context)
    _raw_token, token = auth.create_token(
        "workspace-shell",
        scopes=[AuthScope.Write.value],
    )
    service, fake = _ticket_service(isolated_services)

    with pytest.raises(AuthorizationDeniedError, match="missing scope"):
        service.mint_shell_ticket(token, audience=_audience(token.workspace_id))
    assert not fake.values

    other_workspace = owned_workspace(ControlPlaneService(isolated_services.context), "other")
    _other_raw, other = auth.create_workspace_token(
        other_workspace.id,
        name="other-shell",
        scopes=[AuthScope.Read.value],
    )
    with pytest.raises(AuthorizationDeniedError, match="workspace"):
        service.mint_shell_ticket(other, audience=_audience(token.workspace_id))
    assert not fake.values


def test_ticket_mint_removes_uncertain_write_and_hides_store_details(
    isolated_services: ApiServices,
) -> None:
    class WriteThenFailRedis(FakeRedis):
        def set(
            self,
            name: str,
            value: RedisWireScalar,
            *,
            ex: int | None = None,
            px: int | None = None,
            nx: bool = False,
        ) -> bool:
            super().set(name, value, ex=ex, px=px, nx=nx)
            raise OSError("private redis endpoint")

    _raw_token, token = AuthService(isolated_services.context).create_token(
        "browser-shell",
        scopes=[AuthScope.Read.value],
    )
    fake = WriteThenFailRedis()
    service = WebSocketTicketService(
        isolated_services.context,
        RedisClient(fake, key_prefix="ticket-test"),
    )

    with pytest.raises(WebSocketTicketStoreError) as raised:
        service.mint_shell_ticket(token, audience=_audience(token.workspace_id))

    assert str(raised.value) == "WebSocket ticket storage is temporarily unavailable"
    assert not fake.values


@pytest.mark.skipif(
    not os.environ.get("LAZYCLOUD_TEST_REDIS_URL"),
    reason="set LAZYCLOUD_TEST_REDIS_URL to run real Redis ticket semantics",
)
def test_real_redis_getdel_is_atomic_and_expiry_is_terminal() -> None:
    redis = RedisClient.from_settings(
        RedisSettings(
            url=os.environ["LAZYCLOUD_TEST_REDIS_URL"],
            key_prefix=f"ticket-real-{uuid4()}",
        )
    )
    key = redis.key("single-use")
    expiring_key = redis.key("expires")
    try:
        assert redis.set_single_use(key, "payload", ttl_seconds=30)

        def consume_once(_index: int) -> str | None:
            return redis.getdel(key)

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(consume_once, range(24)))
        assert results.count("payload") == 1
        assert results.count(None) == 23

        assert redis.set_single_use(expiring_key, "payload", ttl_seconds=1)
        time.sleep(1.1)
        assert redis.getdel(expiring_key) is None
    finally:
        redis.delete(key)
        redis.delete(expiring_key)
        redis.close()

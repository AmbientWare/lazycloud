from __future__ import annotations

import hashlib
import secrets
from contextlib import suppress
from dataclasses import dataclass

from coordination.redis_client import RedisClient
from pydantic import ValidationError
from shared.contracts import ContractModel
from shared.identity import AuthScope, AuthTokenRecord

from identity.auth import AuthError, AuthorizationDeniedError, AuthService, IdentityContext
from identity.authz import decide_authorization, workspace_requirement

WEBSOCKET_TICKET_TTL_SECONDS = 30
_WEBSOCKET_TICKET_PREFIX = "wst_"
_WEBSOCKET_TICKET_KEY_NAMESPACE = "identity:websocket-ticket"


class ShellWebSocketAudience(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str


class WebSocketTicketStoreError(RuntimeError):
    pass


class _WebSocketTicketPayload(ContractModel):
    token_id: str
    token_workspace_id: str
    required_scope: AuthScope
    audience: ShellWebSocketAudience


@dataclass(frozen=True, slots=True)
class ShellWebSocketAuthorization:
    token: AuthTokenRecord
    audience: ShellWebSocketAudience


@dataclass(slots=True)
class WebSocketTicketService:
    context: IdentityContext
    redis: RedisClient

    def mint_shell_ticket(
        self,
        token: AuthTokenRecord,
        *,
        audience: ShellWebSocketAudience,
        required_scope: AuthScope = AuthScope.Read,
    ) -> str:
        requirement = workspace_requirement(
            audience.workspace_id,
            action=required_scope,
        )
        decision = decide_authorization(token, requirement)
        if not decision.allowed:
            raise AuthorizationDeniedError(decision.message)
        payload = _WebSocketTicketPayload(
            token_id=token.id,
            token_workspace_id=token.workspace_id,
            required_scope=required_scope,
            audience=audience,
        ).model_dump_json()
        for _attempt in range(3):
            ticket = f"{_WEBSOCKET_TICKET_PREFIX}{secrets.token_urlsafe(32)}"
            ticket_key = self._ticket_key(ticket)
            try:
                stored = self.redis.set_single_use(
                    ticket_key,
                    payload,
                    ttl_seconds=WEBSOCKET_TICKET_TTL_SECONDS,
                )
            except Exception as exc:
                with suppress(Exception):
                    self.redis.delete(ticket_key)
                raise WebSocketTicketStoreError(
                    "WebSocket ticket storage is temporarily unavailable"
                ) from exc
            if stored:
                return ticket
        raise WebSocketTicketStoreError("WebSocket ticket storage is temporarily unavailable")

    def consume_shell_ticket(
        self,
        ticket: str,
        *,
        stub_id: str,
        container_id: str,
        required_scope: AuthScope = AuthScope.Read,
    ) -> ShellWebSocketAuthorization:
        # GETDEL is intentionally first. Invalid payloads, wrong audiences, and
        # failed durable authorization all consume the credential permanently.
        encoded = self.redis.getdel(self._ticket_key(ticket))
        if encoded is None:
            raise AuthError("invalid or expired WebSocket ticket")
        try:
            payload = _WebSocketTicketPayload.model_validate_json(encoded)
        except ValidationError as exc:
            raise AuthError("invalid WebSocket ticket") from exc
        if payload.audience.stub_id != stub_id or payload.audience.container_id != container_id:
            raise AuthError("WebSocket ticket audience does not match")
        if payload.required_scope is not required_scope:
            raise AuthError("WebSocket ticket scope does not match")
        token = AuthService(self.context).authorize_token_identity(
            payload.token_id,
            token_workspace_id=payload.token_workspace_id,
            requirement=workspace_requirement(
                payload.audience.workspace_id,
                action=payload.required_scope,
            ),
        )
        return ShellWebSocketAuthorization(token=token, audience=payload.audience)

    def _ticket_key(self, ticket: str) -> str:
        digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        return self.redis.key(_WEBSOCKET_TICKET_KEY_NAMESPACE, digest)


__all__ = [
    "WEBSOCKET_TICKET_TTL_SECONDS",
    "ShellWebSocketAudience",
    "ShellWebSocketAuthorization",
    "WebSocketTicketService",
    "WebSocketTicketStoreError",
]

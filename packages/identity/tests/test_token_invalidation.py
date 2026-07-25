from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisWireScalar
from database.context import ServiceContext
from identity.auth import AuthError, AuthService
from identity.token_invalidation import (
    AuthTokenInvalidation,
)
from identity.workspaces import WorkspaceDeletionIdentityService
from redis.exceptions import ConnectionError as RedisConnectionError
from shared.errors import NotFoundError
from shared.identity import TokenKind
from tests.redis_fakes import FakeRedis


def _replica_context(services: ApiServices) -> ServiceContext:
    """A second process's view of the same durable state: fresh identity, same database."""
    return ServiceContext(database=services.context.database, paths=services.context.paths)


def test_local_cache_alone_serves_stale_token_without_invalidation_signal(
    isolated_services: ApiServices,
) -> None:
    auth_a = AuthService(isolated_services.context)
    auth_b = AuthService(_replica_context(isolated_services))
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id
    auth_a.revoke_token(record.id)

    # Replica B still trusts its warm local cache inside the TTL window: this is
    # exactly the staleness the invalidation signal exists to eliminate.
    assert auth_b.authenticate(raw_token).id == record.id


def test_revoked_token_rejected_immediately_across_replicas(
    isolated_services: ApiServices,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(isolated_services.redis_client)
    auth_a = AuthService(isolated_services.context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(isolated_services), token_invalidation=invalidation)
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id
    auth_a.revoke_token(record.id)

    with pytest.raises(AuthError):
        auth_b.authenticate(raw_token)


def test_admin_disable_rejected_immediately_across_replicas(
    isolated_services: ApiServices,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(isolated_services.redis_client)
    auth_a = AuthService(isolated_services.context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(isolated_services), token_invalidation=invalidation)
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id
    auth_a.set_workspace_tokens_admin_disabled(record.workspace_id, disabled=True)

    with pytest.raises(AuthError):
        auth_b.authenticate(raw_token)


def test_every_validity_mutation_emits_invalidation(isolated_services: ApiServices) -> None:
    invalidation = AuthTokenInvalidation.from_redis(isolated_services.redis_client)
    auth = AuthService(isolated_services.context, token_invalidation=invalidation)

    def generation() -> int:
        value = invalidation.current_generation()
        assert value is not None
        return value

    baseline = generation()
    _raw_token, record = auth.create_token("api-key")
    assert generation() == baseline, "creation must not churn replica caches"

    auth.revoke_token(record.id)
    after_revoke = generation()
    assert after_revoke > baseline

    auth.toggle_token(record.id)
    after_toggle = generation()
    assert after_toggle > after_revoke

    auth.toggle_workspace_token(record.workspace_id, record.id)
    after_workspace_toggle = generation()
    assert after_workspace_toggle > after_toggle

    auth.set_workspace_tokens_admin_disabled(record.workspace_id, disabled=True)
    after_disable = generation()
    assert after_disable > after_workspace_toggle
    auth.set_workspace_tokens_admin_disabled(record.workspace_id, disabled=False)

    before_delete = generation()
    auth.delete_workspace_token(record.workspace_id, record.id)
    assert generation() > before_delete
    with pytest.raises(NotFoundError, match="workspace token not found"):
        auth.toggle_workspace_token(record.workspace_id, record.id)

    _raw, expired = auth.create_token(
        "expired-worker",
        kind=TokenKind.Worker,
        expires_in_seconds=-10,
    )
    before_prune = generation()
    assert auth.prune_expired_system_tokens() >= 1
    assert generation() > before_prune
    del expired

    expired_raw, _expired_record = auth.create_token("expired-user", expires_in_seconds=-10)
    before_expiry = generation()
    with pytest.raises(AuthError):
        auth.authenticate(expired_raw)
    assert generation() > before_expiry, "expiry-driven auto-revoke must emit"

    _admin_raw, audit_actor = auth.create_token(
        "workspace-delete-admin",
        kind=TokenKind.Admin,
        workspace_id=record.workspace_id,
    )
    doomed_workspace = ControlPlaneService(isolated_services.context).upsert_workspace(
        "doomed-workspace"
    )
    _raw, other = auth.create_token("other-workspace", workspace_id=doomed_workspace.id)
    before_workspace_delete = generation()
    identity = WorkspaceDeletionIdentityService(isolated_services.context)
    with isolated_services.context.database.session() as session:
        deleting = identity.lock_and_validate_begin(
            session,
            doomed_workspace.id,
            actor_workspace_id=audit_actor.workspace_id,
        )
        identity.mark_deleting(session, deleting)
    auth.workspace_credentials_revoked()
    with isolated_services.context.database.session() as session:
        identity.finalize(session, doomed_workspace.id, actor=audit_actor)
    assert generation() > before_workspace_delete
    del other


def test_redis_outage_bypasses_cache_and_falls_back_to_database(
    isolated_services: ApiServices,
) -> None:
    outage = _OutageRedis()
    invalidation = AuthTokenInvalidation.from_redis(RedisClient(outage, key_prefix="test"))
    auth_a = AuthService(isolated_services.context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(isolated_services), token_invalidation=invalidation)
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id

    outage.down = True
    # Mutations still succeed: the validity change is durable in the database and
    # the failed emit is logged, not raised.
    auth_a.revoke_token(record.id)

    # The warm cache on replica B cannot be verified, so the database decides:
    # the revoked token is rejected immediately even though no emit landed.
    with pytest.raises(AuthError):
        auth_b.authenticate(raw_token)

    # Valid tokens keep authenticating through the database during the outage.
    other_raw, other_record = auth_a.create_token("still-valid")
    assert auth_b.authenticate(other_raw).id == other_record.id
    assert auth_b.authenticate(other_raw).id == other_record.id

    outage.down = False
    assert auth_b.authenticate(other_raw).id == other_record.id


class _OutageRedis(FakeRedis):
    """Script-free fake that simulates an outage for invalidation commands."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def get(self, name: str) -> RedisWireScalar | None:
        if self.down:
            raise RedisConnectionError("redis unavailable")
        return super().get(name)

    def incr(self, name: str) -> int:
        if self.down:
            raise RedisConnectionError("redis unavailable")
        return super().incr(name)

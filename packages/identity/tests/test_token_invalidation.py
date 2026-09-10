from __future__ import annotations

import pytest
from coordination.redis_client import RedisClient, RedisWireScalar
from database.context import ServiceContext
from identity.auth import AuthError, AuthService
from identity.token_invalidation import (
    AuthTokenInvalidation,
)
from identity.users import UserService
from redis.exceptions import ConnectionError as RedisConnectionError
from tests.real_redis import RealRedisActors
from tests.redis_fakes import FakeRedis


def _replica_context(context: ServiceContext) -> ServiceContext:
    """A second process's view of the same durable state: fresh identity, same database."""
    return ServiceContext(database=context.database, paths=context.paths)


def test_revoked_token_rejected_immediately_across_replicas(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(real_redis_actors.client())
    auth_a = AuthService(service_context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(service_context), token_invalidation=invalidation)
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id
    auth_a.revoke_token(record.id)

    with pytest.raises(AuthError):
        auth_b.authenticate(raw_token)


def test_admin_disable_rejected_immediately_across_replicas(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(real_redis_actors.client())
    auth_a = AuthService(service_context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(service_context), token_invalidation=invalidation)
    raw_token, record = auth_a.create_token("api-key")

    assert auth_b.authenticate(raw_token).id == record.id
    auth_a.set_workspace_tokens_admin_disabled(record.workspace_id, disabled=True)

    with pytest.raises(AuthError):
        auth_b.authenticate(raw_token)


def test_revoked_account_token_is_rejected_by_a_warm_replica(
    service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
) -> None:
    invalidation = AuthTokenInvalidation.from_redis(real_redis_actors.client())
    first = AuthService(service_context, token_invalidation=invalidation)
    second = AuthService(_replica_context(service_context), token_invalidation=invalidation)
    owner = UserService(service_context).create(display_name="account-owner")
    raw_token, record = first.create_account_token(owner.id, "account-key")
    assert second.authenticate(raw_token).id == record.id

    first.revoke_account_token(owner.id, record.id)

    with pytest.raises(AuthError):
        second.authenticate(raw_token)


def test_redis_outage_bypasses_cache_and_falls_back_to_database(
    service_context: ServiceContext,
) -> None:
    outage = _OutageRedis()
    invalidation = AuthTokenInvalidation.from_redis(RedisClient(outage, key_prefix="test"))
    auth_a = AuthService(service_context, token_invalidation=invalidation)
    auth_b = AuthService(_replica_context(service_context), token_invalidation=invalidation)
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

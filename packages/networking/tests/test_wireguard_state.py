from __future__ import annotations

from coordination.token_lock import (
    TokenLockReleaseStatus,
    release_token_lock,
    try_acquire_token_lock,
)
from networking.wireguard_state import (
    WireGuardGatewayPresence,
    WireGuardGatewayPresenceRepository,
    WireGuardPeerPresence,
)
from tests.real_redis import RealRedisActors


def test_gateway_presence_excludes_and_fences_previous_lease_owner(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    repository = WireGuardGatewayPresenceRepository(redis)
    lease_key = repository.lease_key(0)
    original = WireGuardGatewayPresence(
        index=0,
        owner_token="original",
        peers=(WireGuardPeerPresence(peer_id="peer", generation=1),),
    )
    replacement = WireGuardGatewayPresence(
        index=0,
        owner_token="replacement",
        peers=(WireGuardPeerPresence(peer_id="peer", generation=2),),
    )
    assert try_acquire_token_lock(redis, lease_key, original.owner_token, ttl_seconds=30)
    assert repository.publish(original)
    assert repository.list_current((0, 1)) == (original,)
    assert (
        release_token_lock(redis, lease_key, original.owner_token)
        is TokenLockReleaseStatus.Released
    )
    assert repository.list_current((0, 1)) == ()

    assert try_acquire_token_lock(redis, lease_key, replacement.owner_token, ttl_seconds=30)
    assert not repository.publish(original)
    assert not repository.remove(0, original.owner_token)
    assert repository.list_current((0,)) == ()
    assert repository.publish(replacement)
    assert not repository.publish(original)
    assert not repository.remove(0, original.owner_token)
    assert repository.list_current((0, 1)) == (replacement,)
    assert repository.remove(0, replacement.owner_token)
    assert repository.list_current((0,)) == ()
    assert (
        release_token_lock(redis, lease_key, replacement.owner_token)
        is TokenLockReleaseStatus.Released
    )

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from coordination.agent_connections import RedisAgentConnectionDirectory
from shared.agent_connections import AgentConnectionRecord
from shared.http.agent_identity import AgentTunnelIdentity
from tests.real_redis import RealRedisActors


def test_agent_connection_claims_fence_competing_owners_and_credential_generations(
    real_redis_actors: RealRedisActors,
) -> None:
    directories = [RedisAgentConnectionDirectory(real_redis_actors.client()) for _ in range(2)]
    identity = AgentTunnelIdentity(
        workspace_id=str(uuid4()), enrollment_id=str(uuid4()), credential_generation=2
    )
    records = [
        AgentConnectionRecord(
            identity=identity,
            gateway_id=str(uuid4()),
            connection_id=str(uuid4()),
            gateway_address=f"gateway-{index}.internal:9443",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = [
            executor.submit(directory.claim, record)
            for directory, record in zip(directories, records, strict=True)
        ]
        accepted = [claim.result() for claim in claims]
    assert accepted.count(True) == 1
    winner = records[accepted.index(True)]
    directory = directories[0]
    assert directory.get(identity.workspace_id, identity.enrollment_id) == winner

    older = winner.model_copy(
        update={
            "connection_id": str(uuid4()),
            "identity": identity.model_copy(update={"credential_generation": 1}),
        }
    )
    assert not directory.claim(older, previous_connection_id=winner.connection_id)
    replacements = [record.model_copy(update={"connection_id": str(uuid4())}) for record in records]
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = [
            executor.submit(directory.claim, record, winner.connection_id)
            for directory, record in zip(directories, replacements, strict=True)
        ]
        accepted = [claim.result() for claim in claims]
    assert accepted.count(True) == 1
    replacement = replacements[accepted.index(True)]
    assert not directory.renew(winner)
    assert not directory.release(winner)
    changed_gateway = replacement.model_copy(update={"gateway_id": str(uuid4())})
    assert not directory.renew(changed_gateway)
    assert not directory.release(changed_gateway)
    assert directory.renew(replacement)

    rotated = replacement.model_copy(
        update={
            "connection_id": str(uuid4()),
            "identity": identity.model_copy(update={"credential_generation": 3}),
        }
    )
    assert directory.claim(rotated, previous_connection_id=replacement.connection_id)
    assert not directory.claim(replacement, previous_connection_id=rotated.connection_id)
    assert directory.get(str(uuid4()), identity.enrollment_id) is None
    assert directory.get(identity.workspace_id, str(uuid4())) is None
    assert directory.release(rotated)
    assert not directory.release(rotated)


def test_agent_connection_reset_and_certificate_expiry_preserve_new_ownership(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client(decode_responses=False)
    directory = RedisAgentConnectionDirectory(redis)
    record = AgentConnectionRecord(
        identity=AgentTunnelIdentity(
            workspace_id=str(uuid4()), enrollment_id=str(uuid4()), credential_generation=1
        ),
        gateway_id=str(uuid4()),
        connection_id=str(uuid4()),
        gateway_address="[::1]:9443",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert directory.claim(record)
    keys = redis.scan(redis.key("agent-connections", "*"))
    assert len(keys) == 1
    assert 0 < redis.ttl(keys[0]) <= 6
    redis.delete(keys[0])
    assert not directory.renew(record)
    assert not directory.claim(record, previous_connection_id=record.connection_id)

    replacement = record.model_copy(update={"connection_id": str(uuid4())})
    assert directory.claim(replacement)
    assert not directory.release(record)
    assert not directory.renew(record)
    assert directory.get(record.identity.workspace_id, record.identity.enrollment_id) == replacement
    assert directory.release(replacement)

    expired = record.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert not directory.claim(expired)
    expiring = record.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=4)})
    assert directory.claim(expiring)
    assert directory.renew(expiring)
    assert 0 < redis.eval_int("return redis.call('PTTL', KEYS[1])", 1, keys[0]) <= 4000
    assert not directory.renew(record)
    assert directory.release(expiring)


@pytest.mark.parametrize(
    "address",
    ["https://gateway.internal:9443", "user@gateway.internal:9443", "gateway.internal:9443/path"],
)
def test_agent_connection_rejects_gateway_url_syntax(address: str) -> None:
    with pytest.raises(ValueError, match="gateway address"):
        AgentConnectionRecord(
            identity=AgentTunnelIdentity(
                workspace_id=str(uuid4()), enrollment_id=str(uuid4()), credential_generation=1
            ),
            gateway_id=str(uuid4()),
            connection_id=str(uuid4()),
            gateway_address=address,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

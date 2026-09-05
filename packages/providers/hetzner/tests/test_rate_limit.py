from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from coordination.request_cooldown import RedisRequestCooldown
from provider_hetzner.client import HetznerClient, HetznerError
from pydantic import SecretStr
from tests.real_redis import RealRedisActors


def test_rate_limit_headers_block_other_runtime_clients(
    real_redis_actors: RealRedisActors, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
    retry_after = reset + timedelta(minutes=1)

    def response(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        del transport, request
        return httpx.Response(
            429,
            headers={
                "RateLimit-Reset": str(int(reset.timestamp())),
                "Retry-After": format_datetime(retry_after, usegmt=True),
            },
            json={"error": {"code": "rate_limit_exceeded"}},
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", response)
    first = HetznerClient(
        SecretStr("test-token"), RedisRequestCooldown(real_redis_actors.client(), "hetzner:project")
    )
    second = HetznerClient(
        SecretStr("test-token"), RedisRequestCooldown(real_redis_actors.client(), "hetzner:project")
    )
    with pytest.raises(HetznerError) as original:
        first.server(1)
    assert original.value.code == "rate_limit_exceeded"
    assert original.value.retry_at == retry_after
    with pytest.raises(HetznerError) as blocked:
        second.server(1)
    assert blocked.value.code == "rate_limit_cooldown"
    assert blocked.value.retry_at == retry_after

from __future__ import annotations

from dataclasses import dataclass, field

from coordination.redis_client import RedisClient, RedisSettings


@dataclass(slots=True)
class RealRedisActors:
    url: str
    prefix: str
    clients: list[RedisClient] = field(default_factory=list)

    def client(self, *, decode_responses: bool = True) -> RedisClient:
        client = RedisClient.from_settings(
            RedisSettings(
                url=self.url,
                key_prefix=self.prefix,
                decode_responses=decode_responses,
                socket_timeout_seconds=2.0,
                health_check_interval_seconds=1,
            )
        )
        assert client.ping()
        self.clients.append(client)
        return client

    def cleanup(self) -> None:
        cleanup_client = self.clients[0] if self.clients else self.client()
        pattern = f"{self.prefix}:*"
        keys = cleanup_client.scan(pattern)
        if keys:
            cleanup_client.delete(*keys)
        remaining = cleanup_client.scan(pattern)
        for client in self.clients:
            client.close()
        assert remaining == [], f"real Redis test leaked keys: {remaining}"

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class EndpointAddress:
    scheme: str
    host: str
    port: int | None


def resolve_endpoint(endpoint: str) -> EndpointAddress:
    parsed = urlparse(endpoint)
    return EndpointAddress(
        scheme=parsed.scheme or "http",
        host=parsed.hostname or parsed.path or "localhost",
        port=parsed.port,
    )

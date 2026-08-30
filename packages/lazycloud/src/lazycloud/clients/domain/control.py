from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from shared.custom_domains import CustomDomainDnsMode
from shared.http.custom_domains import (
    CustomDomainListResponse,
    CustomDomainRegisterRequest,
    CustomDomainResponse,
)
from shared.http_transport import HttpChannel

COLLECTION_PATH = "/api/v1/custom-domains"


class DomainControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class DomainControlClient:
    channel: DomainControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> DomainControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def register(
        self,
        domain: str,
        *,
        dns_mode: CustomDomainDnsMode,
    ) -> CustomDomainResponse:
        request = CustomDomainRegisterRequest(domain=domain, dns_mode=dns_mode)
        return CustomDomainResponse.model_validate(
            self.channel.post(COLLECTION_PATH, request.model_dump(mode="json"))
        )

    def list(self) -> CustomDomainListResponse:
        return CustomDomainListResponse.model_validate(self.channel.get(COLLECTION_PATH))

    def get(self, hostname: str) -> CustomDomainResponse:
        return CustomDomainResponse.model_validate(self.channel.get(self._path(hostname)))

    def remove(self, hostname: str) -> None:
        self.channel.delete(self._path(hostname))

    @staticmethod
    def _path(hostname: str) -> str:
        # A registered domain may be a wildcard, and `*` is not path-safe.
        return f"{COLLECTION_PATH}/{quote(hostname, safe='')}"


__all__ = ["COLLECTION_PATH", "DomainControlClient"]

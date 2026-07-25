from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.http.secrets import (
    CreateSecretResponse,
    DeleteSecretResponse,
    GetSecretResponse,
    ListSecretsResponse,
    SecretMaskedSetResponse,
    UpdateSecretResponse,
)
from shared.http_transport import HttpChannel


class SecretControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def patch(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def delete(self, path: str) -> Any: ...


@dataclass
class SecretControlClient:
    channel: SecretControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> SecretControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def create(self, name: str, value: str) -> CreateSecretResponse:
        return CreateSecretResponse.model_validate(
            self.channel.post(
                self._collection_path(),
                {"name": name, "value": value},
            )
        )

    def get(self, name: str) -> GetSecretResponse:
        return GetSecretResponse.model_validate(self.channel.get(self._item_path(name)))

    def list(self) -> ListSecretsResponse:
        return ListSecretsResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/secrets/full"))
        )

    def update(self, name: str, value: str) -> UpdateSecretResponse:
        return UpdateSecretResponse.model_validate(
            self.channel.patch(
                self._item_path(name),
                {"value": value},
            )
        )

    def set(self, name: str, value: str) -> SecretMaskedSetResponse:
        return SecretMaskedSetResponse.model_validate(
            self.channel.post(
                self._item_path(name),
                {"value": value},
            )
        )

    def delete(self, name: str) -> DeleteSecretResponse:
        return DeleteSecretResponse.model_validate(self.channel.delete(self._item_path(name)))

    def _collection_path(self) -> str:
        return self._workspace_path("/api/v1/secrets")

    def _item_path(self, name: str) -> str:
        return self._workspace_path(f"/api/v1/secrets/{quote(name, safe='')}")

    def _workspace_path(self, path: str) -> str:
        return f"{path}?{urlencode({'workspace': self.workspace})}"


__all__ = [
    "SecretControlChannel",
    "SecretControlClient",
]

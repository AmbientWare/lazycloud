from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from shared.http.errors import HttpApiError
from shared.http.secrets import (
    CreateSecretResponse,
    DeleteSecretResponse,
    GetSecretResponse,
    SecretMaskedSetResponse,
    SecretWireRecord,
    UpdateSecretResponse,
)
from shared.secrets import SecretRecord

from lazycloud.control import (
    ControlClientConfig,
    ResourceControlBinding,
    resolve_control_client_config,
)


class SecretClient(Protocol):
    def create(self, name: str, value: str) -> CreateSecretResponse: ...

    def get(self, name: str) -> GetSecretResponse: ...

    def update(self, name: str, value: str) -> UpdateSecretResponse: ...

    def set(self, name: str, value: str) -> SecretMaskedSetResponse: ...

    def delete(self, name: str) -> DeleteSecretResponse: ...


class SecretOperationError(RuntimeError):
    pass


@dataclass(slots=True)
class Secret(ResourceControlBinding[SecretClient]):
    name: str
    workspace: str | None = None
    client: SecretClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    @property
    def control_client(self) -> SecretClient:
        if self.client is None:
            config = resolve_control_client_config(
                workspace=self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = _default_secret_client(config)
        return self.client

    def create(self, value: str) -> SecretRecord:
        try:
            self.control_client.create(self.name, value)
        except HttpApiError as exc:
            raise SecretOperationError(
                exc.detail or str(exc) or f"failed to create secret: {self.name}"
            ) from exc
        return self.record()

    def update(self, value: str) -> SecretRecord:
        try:
            self.control_client.update(self.name, value)
        except HttpApiError as exc:
            raise SecretOperationError(
                exc.detail or str(exc) or f"failed to update secret: {self.name}"
            ) from exc
        return self.record()

    def set(self, value: str) -> SecretRecord:
        try:
            self.control_client.set(self.name, value)
        except HttpApiError as exc:
            raise SecretOperationError(
                exc.detail or str(exc) or f"failed to set secret: {self.name}"
            ) from exc
        return self.record()

    def get(self) -> str:
        return self.record().value

    def record(self) -> SecretRecord:
        try:
            response = self.control_client.get(self.name)
        except HttpApiError as exc:
            raise SecretOperationError(
                exc.detail or str(exc) or f"secret not found: {self.name}"
            ) from exc
        if response.secret is None:
            raise SecretOperationError(f"secret not found: {self.name}")
        return _secret_record(response.secret)

    def delete(self) -> bool:
        try:
            self.control_client.delete(self.name)
        except HttpApiError as exc:
            raise SecretOperationError(
                exc.detail or str(exc) or f"failed to delete secret: {self.name}"
            ) from exc
        return True


def _default_secret_client(config: ControlClientConfig) -> SecretClient:
    from lazycloud.clients.secret.control import SecretControlClient

    return SecretControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _secret_record(record: SecretWireRecord) -> SecretRecord:
    return SecretRecord(
        name=record.name,
        value=record.value,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


__all__ = [
    "Secret",
    "SecretClient",
    "SecretOperationError",
]

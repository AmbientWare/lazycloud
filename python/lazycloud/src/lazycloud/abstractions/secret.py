from __future__ import annotations

from dataclasses import dataclass, field

from lazycloud._shared.secrets import SecretRecord
from lazycloud.clients.api import ApiClient
from lazycloud.control import (
    ResourceControlBinding,
    api_client,
    require_workspace,
    resolve_control_client_config,
)
from lazycloud.exceptions import SdkError


class SecretOperationError(RuntimeError):
    pass


@dataclass(slots=True)
class Secret(ResourceControlBinding[ApiClient]):
    """A workspace secret. Workloads that list it in `secrets=` receive its
    value as an environment variable of the same name."""

    name: str
    workspace: str | None = None
    client: ApiClient | None = field(default=None, init=False, repr=False)
    endpoint: str | None = field(default=None, init=False, repr=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False, repr=False)

    def _session(self) -> tuple[ApiClient, str]:
        config = resolve_control_client_config(
            workspace=self.workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )
        if self.client is None:
            self.client = api_client(config)
        return self.client, require_workspace(config)

    def create(self, value: str) -> SecretRecord:
        """Create the secret; an existing one is an error."""
        client, workspace = self._session()
        try:
            client.create_secret(workspace, self.name, value)
        except SdkError as exc:
            raise SecretOperationError(
                _message(exc, f"failed to create secret: {self.name}")
            ) from exc
        return self.record()

    def update(self, value: str) -> SecretRecord:
        """Replace an existing secret's value."""
        client, workspace = self._session()
        try:
            client.update_secret(workspace, self.name, value)
        except SdkError as exc:
            raise SecretOperationError(
                _message(exc, f"failed to update secret: {self.name}")
            ) from exc
        return self.record()

    def set(self, value: str) -> SecretRecord:
        """Create the secret or replace its value."""
        client, workspace = self._session()
        try:
            client.set_secret(workspace, self.name, value)
        except SdkError as exc:
            raise SecretOperationError(_message(exc, f"failed to set secret: {self.name}")) from exc
        return self.record()

    def get(self) -> str:
        return self.record().value

    def record(self) -> SecretRecord:
        client, workspace = self._session()
        try:
            secret = client.get_secret_value(workspace, self.name)
        except SdkError as exc:
            raise SecretOperationError(_message(exc, f"secret not found: {self.name}")) from exc
        return SecretRecord(
            name=secret.name,
            value=secret.value,
            created_at=secret.created_at,
            updated_at=secret.updated_at,
        )

    def delete(self) -> bool:
        client, workspace = self._session()
        try:
            client.delete_secret(workspace, self.name)
        except SdkError as exc:
            raise SecretOperationError(
                _message(exc, f"failed to delete secret: {self.name}")
            ) from exc
        return True


def _message(exc: SdkError, fallback: str) -> str:
    message = getattr(exc, "message", "") or str(exc)
    return message or fallback


__all__ = [
    "Secret",
    "SecretOperationError",
]

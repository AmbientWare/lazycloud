from __future__ import annotations

from pydantic import ConfigDict, SecretStr, field_serializer

from shared.contracts import ContractModel


class HttpModel(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        validate_assignment=True,
    )


class SecretRequestModel(HttpModel):
    """A request whose secret fields have to reach the server as themselves.

    `SecretStr` serializes to its mask, which is what a response wants and the
    opposite of what a request wants: a client that dumps one sends `**********`
    as the value, and the server hashes or checks that string as if the caller had
    chosen it. Every model carrying a secret inbound derives from here, so no
    client can serialize one wrongly.
    """

    @field_serializer("*", when_used="always")
    def _reveal_secret(self, value: object) -> object:
        return value.get_secret_value() if isinstance(value, SecretStr) else value


__all__ = ["HttpModel", "SecretRequestModel"]

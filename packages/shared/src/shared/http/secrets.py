from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.http.base import HttpModel
from shared.http.storage import ResourceWorkloadReference
from shared.secrets import SecretRecord


class SecretPayload(HttpModel):
    name: str
    value: str


class SecretValuePayload(HttpModel):
    value: str


class SecretMaskedRecord(HttpModel):
    name: str
    value: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    workloads: list[ResourceWorkloadReference] = Field(default_factory=list)


class SecretMaskedListResponse(HttpModel):
    secrets: list[SecretMaskedRecord] = Field(default_factory=list)


class SecretMaskedSetResponse(HttpModel):
    name: str
    value: str


class SecretDeleteResponse(HttpModel):
    pass


class SecretWireRecord(HttpModel):
    id: str
    name: str
    value: str
    last_updated_by: str = ""
    created_at: datetime
    updated_at: datetime
    workloads: list[ResourceWorkloadReference] = Field(default_factory=list)

    @classmethod
    def from_record(
        cls,
        record: SecretRecord,
        *,
        workloads: tuple[ResourceWorkloadReference, ...] = (),
    ) -> SecretWireRecord:
        return cls(
            id=record.name,
            name=record.name,
            value=record.value,
            created_at=record.created_at,
            updated_at=record.updated_at,
            workloads=list(workloads),
        )


class CreateSecretResponse(HttpModel):
    id: str = ""
    name: str = ""

    @classmethod
    def from_record(cls, record: SecretRecord) -> CreateSecretResponse:
        return cls(id=record.name, name=record.name)


class DeleteSecretResponse(HttpModel):
    pass


class UpdateSecretResponse(HttpModel):
    pass


class GetSecretResponse(HttpModel):
    secret: SecretWireRecord | None = None

    @classmethod
    def from_record(cls, record: SecretRecord) -> GetSecretResponse:
        return cls(secret=SecretWireRecord.from_record(record))


class ListSecretsResponse(HttpModel):
    secrets: list[SecretWireRecord] = Field(default_factory=list)

    @classmethod
    def from_records(
        cls,
        records: list[SecretRecord],
        *,
        workloads: dict[str, tuple[ResourceWorkloadReference, ...]] | None = None,
    ) -> ListSecretsResponse:
        relationships = workloads or {}
        return cls(
            secrets=[
                SecretWireRecord.from_record(
                    record,
                    workloads=relationships.get(record.name, ()),
                )
                for record in records
            ]
        )


__all__ = [
    "CreateSecretResponse",
    "DeleteSecretResponse",
    "GetSecretResponse",
    "ListSecretsResponse",
    "SecretDeleteResponse",
    "SecretMaskedListResponse",
    "SecretMaskedRecord",
    "SecretMaskedSetResponse",
    "SecretPayload",
    "SecretValuePayload",
    "SecretWireRecord",
    "UpdateSecretResponse",
]

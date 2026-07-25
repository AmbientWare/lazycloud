from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from shared.contracts import ContractModel


class ManagedBillingMode(StrEnum):
    Disabled = "disabled"
    Noop = "noop"
    Http = "http"


class ManagedBillingSettings(ContractModel):
    mode: ManagedBillingMode = ManagedBillingMode.Noop
    endpoint: str = ""
    auth_token: str = ""
    timeout_seconds: float = Field(default=10.0, gt=0)
    minimum_credit_cents: int = Field(default=0, ge=0)
    required: bool = False
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def normalize_endpoint(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        names: set[str] = set()
        for raw_name, raw_value in value.items():
            name = raw_name.strip()
            header_value = raw_value.strip()
            if not name:
                raise ValueError("managed billing header names cannot be empty")
            if "\r" in name or "\n" in name or "\r" in header_value or "\n" in header_value:
                raise ValueError("managed billing headers cannot contain line breaks")
            canonical_name = name.casefold()
            if canonical_name in names:
                raise ValueError(f"duplicate managed billing header: {name}")
            names.add(canonical_name)
            normalized[name] = header_value
        return normalized

    @model_validator(mode="after")
    def validate_http_configuration(self) -> ManagedBillingSettings:
        if self.required and self.mode is ManagedBillingMode.Disabled:
            raise ValueError("required managed billing cannot be disabled")
        if (self.required or self.mode is ManagedBillingMode.Http) and not self.endpoint:
            raise ValueError("managed billing endpoint is required")
        if self.endpoint:
            parsed = urlparse(self.endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("managed billing endpoint must be an absolute HTTP(S) URL")
        return self


__all__ = ["ManagedBillingMode", "ManagedBillingSettings"]

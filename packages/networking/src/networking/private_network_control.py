from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from shared.app_identity import AGENT_NAME


class PrivateNetworkControlErrorCode(StrEnum):
    InvalidConfiguration = "invalid_configuration"
    AuthenticationFailed = "authentication_failed"
    PermissionDenied = "permission_denied"
    NotFound = "not_found"
    Conflict = "conflict"
    RateLimited = "rate_limited"
    UpstreamUnavailable = "upstream_unavailable"
    InvalidResponse = "invalid_response"
    VerificationFailed = "verification_failed"


class PrivateNetworkControlError(RuntimeError):
    def __init__(
        self,
        code: PrivateNetworkControlErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class PrivateNetworkSite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    online: bool = False


class PrivateNetworkCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    site_id: str = Field(min_length=1)
    connector_id: str = Field(min_length=1)
    secret: SecretStr


class PrivateNetworkActiveSite(PrivateNetworkSite):
    resource_id: str = Field(min_length=1)
    address: str = Field(min_length=1)


class PrivateNetworkIdentityCleanup(Protocol):
    def delete_resource(self, resource_id: str) -> None: ...

    def delete_site(self, site_id: str) -> None: ...


class PrivateNetworkControl(PrivateNetworkIdentityCleanup, Protocol):
    def create_site(self, *, name: str) -> PrivateNetworkCredential: ...

    def find_site(
        self,
        *,
        site_id: str,
        name: str,
        connector_id: str,
    ) -> PrivateNetworkSite | None: ...

    def activate_site(self, site_id: str) -> PrivateNetworkActiveSite: ...


@dataclass(frozen=True, slots=True)
class PrivateNetworkMachineIdentityReconciler:
    control: PrivateNetworkIdentityCleanup

    def cleanup(
        self,
        *,
        resource_ids: tuple[str, ...] = (),
        site_ids: tuple[str, ...],
    ) -> int:
        normalized_resources = tuple(
            dict.fromkeys(
                resource_id.strip() for resource_id in resource_ids if resource_id.strip()
            )
        )
        normalized_sites = tuple(
            dict.fromkeys(site_id.strip() for site_id in site_ids if site_id.strip())
        )
        for resource_id in normalized_resources:
            self.control.delete_resource(resource_id)
        for site_id in normalized_sites:
            self.control.delete_site(site_id)
        return len(normalized_resources) + len(normalized_sites)


def private_network_machine_name(machine_id: str, generation: int) -> str:
    machine = machine_id.strip()
    if not machine:
        raise PrivateNetworkControlError(
            PrivateNetworkControlErrorCode.InvalidConfiguration,
            "machine ID is required",
            retryable=False,
        )
    if generation < 1:
        raise PrivateNetworkControlError(
            PrivateNetworkControlErrorCode.InvalidConfiguration,
            "private-network generation must be positive",
            retryable=False,
        )
    return f"{AGENT_NAME}-{machine}-g{generation}"


__all__ = [
    "PrivateNetworkActiveSite",
    "PrivateNetworkControl",
    "PrivateNetworkControlError",
    "PrivateNetworkControlErrorCode",
    "PrivateNetworkCredential",
    "PrivateNetworkIdentityCleanup",
    "PrivateNetworkMachineIdentityReconciler",
    "PrivateNetworkSite",
    "private_network_machine_name",
]

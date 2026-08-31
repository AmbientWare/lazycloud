from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError
from shared.errors import InvalidInputError

from provider_pangolin.client import PangolinClient

PLATFORM_STATE_KEY = "state.json"
PLATFORM_SITE_IDS_KEY = "LAZYCLOUD_PANGOLIN_PLATFORM_SITE_IDS"
PLATFORM_CLIENT_RECORD_IDS_KEY = "LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_RECORD_IDS"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PangolinPlatformSiteCredential(_Model):
    ordinal: int = Field(ge=0)
    site_id: int = Field(gt=0)
    newt_id: str = Field(min_length=1)
    secret: SecretStr


class PangolinPlatformClientCredential(_Model):
    ordinal: int = Field(ge=0)
    client_record_id: int = Field(gt=0)
    olm_id: str = Field(min_length=1)
    secret: SecretStr


class PangolinPlatformCredentials(_Model):
    sites: tuple[PangolinPlatformSiteCredential, ...] = ()
    clients: tuple[PangolinPlatformClientCredential, ...] = ()


class PangolinPlatformCredentialStore(Protocol):
    def load(self) -> PangolinPlatformCredentials: ...

    def save(self, credentials: PangolinPlatformCredentials) -> None: ...


@dataclass(frozen=True, slots=True)
class PangolinPlatformBootstrapResult:
    site_count: int
    client_count: int
    created_site_count: int
    created_client_count: int


@dataclass(slots=True)
class PangolinPlatformBootstrap:
    client: PangolinClient
    store: PangolinPlatformCredentialStore

    def ensure(
        self,
        *,
        site_count: int,
        client_count: int,
        public_hostname: str,
        public_tls: bool,
    ) -> PangolinPlatformBootstrapResult:
        if site_count < 1 or client_count < 1:
            raise InvalidInputError("Pangolin platform site and client counts must be positive")
        credentials = self.store.load()
        self._validate_stored_credentials(credentials)
        if any(item.ordinal >= site_count for item in credentials.sites):
            raise InvalidInputError(
                "Pangolin platform site count cannot be reduced below the stored count"
            )
        if any(item.ordinal >= client_count for item in credentials.clients):
            raise InvalidInputError(
                "Pangolin platform client count cannot be reduced below the stored count"
            )
        created_sites = 0
        created_clients = 0

        for ordinal in range(site_count):
            if _site_credential(credentials, ordinal) is not None:
                continue
            name = self._identity_name(ordinal)
            if self.client.find_site_by_name(name) is not None:
                raise InvalidInputError(
                    f"Pangolin site {name} exists but its credential is unavailable"
                )
            created = self.client.create_site(name=name)
            updated = credentials.model_copy(
                update={
                    "sites": tuple(
                        sorted(
                            (
                                *credentials.sites,
                                PangolinPlatformSiteCredential(
                                    ordinal=ordinal,
                                    site_id=created.site_id,
                                    newt_id=created.newt_id,
                                    secret=created.secret,
                                ),
                            ),
                            key=lambda item: item.ordinal,
                        )
                    )
                }
            )
            self._persist_or_delete_site(updated, created.site_id)
            credentials = updated
            created_sites += 1

        for ordinal in range(client_count):
            if _client_credential(credentials, ordinal) is not None:
                continue
            name = self._identity_name(ordinal)
            if self.client.find_client_by_name(name) is not None:
                raise InvalidInputError(
                    f"Pangolin machine client {name} exists but its credential is unavailable"
                )
            created = self.client.create_client(name=name)
            updated = credentials.model_copy(
                update={
                    "clients": tuple(
                        sorted(
                            (
                                *credentials.clients,
                                PangolinPlatformClientCredential(
                                    ordinal=ordinal,
                                    client_record_id=created.client_id,
                                    olm_id=created.olm_id,
                                    secret=created.secret,
                                ),
                            ),
                            key=lambda item: item.ordinal,
                        )
                    )
                }
            )
            self._persist_or_delete_client(updated, created.client_id)
            credentials = updated
            created_clients += 1

        self.client.synchronize_platform_client_access(
            tuple(item.client_record_id for item in credentials.clients)
        )
        self.client.synchronize_platform_site_targets(
            tuple(item.site_id for item in credentials.sites)
        )
        self.client.ensure_platform_public_resource(
            hostname=public_hostname,
            site_ids=tuple(item.site_id for item in credentials.sites),
            ssl=public_tls,
        )
        return PangolinPlatformBootstrapResult(
            site_count=len(credentials.sites),
            client_count=len(credentials.clients),
            created_site_count=created_sites,
            created_client_count=created_clients,
        )

    def _validate_stored_credentials(self, credentials: PangolinPlatformCredentials) -> None:
        _require_unique_ordinals(credentials)
        for site_credential in credentials.sites:
            site = self.client.get_site(site_credential.site_id)
            if (
                site is None
                or site.name != self._identity_name(site_credential.ordinal)
                or site.newt_id != site_credential.newt_id
            ):
                raise InvalidInputError(
                    f"Pangolin platform site ordinal {site_credential.ordinal} no longer "
                    "matches its stored credential"
                )
        for client_credential in credentials.clients:
            client = self.client.get_client(client_credential.client_record_id)
            if (
                client is None
                or client.name != self._identity_name(client_credential.ordinal)
                or client.olm_id != client_credential.olm_id
            ):
                raise InvalidInputError(
                    f"Pangolin platform client ordinal {client_credential.ordinal} no longer "
                    "matches its stored credential"
                )

    def _persist_or_delete_site(
        self,
        credentials: PangolinPlatformCredentials,
        site_id: int,
    ) -> None:
        try:
            self.store.save(credentials)
        except Exception:
            self.client.delete_site(site_id)
            raise

    def _persist_or_delete_client(
        self,
        credentials: PangolinPlatformCredentials,
        client_id: int,
    ) -> None:
        try:
            self.store.save(credentials)
        except Exception:
            self.client.delete_client(client_id)
            raise

    def _identity_name(self, ordinal: int) -> str:
        return f"{self.client.platform_identity_prefix}-{ordinal}"


def platform_secret_values(credentials: PangolinPlatformCredentials) -> dict[str, str]:
    sites = tuple(sorted(credentials.sites, key=lambda item: item.ordinal))
    clients = tuple(sorted(credentials.clients, key=lambda item: item.ordinal))
    state: JsonValue = {
        "sites": [
            {
                "ordinal": item.ordinal,
                "site_id": item.site_id,
                "newt_id": item.newt_id,
                "secret": item.secret.get_secret_value(),
            }
            for item in sites
        ],
        "clients": [
            {
                "ordinal": item.ordinal,
                "client_record_id": item.client_record_id,
                "olm_id": item.olm_id,
                "secret": item.secret.get_secret_value(),
            }
            for item in clients
        ],
    }
    values = {
        PLATFORM_STATE_KEY: json.dumps(state, separators=(",", ":")),
        PLATFORM_SITE_IDS_KEY: json.dumps([item.site_id for item in sites]),
        PLATFORM_CLIENT_RECORD_IDS_KEY: json.dumps([item.client_record_id for item in clients]),
    }
    for item in sites:
        prefix = f"LAZYCLOUD_PANGOLIN_PLATFORM_CONNECTOR_{item.ordinal}"
        values[f"{prefix}_ID"] = item.newt_id
        values[f"{prefix}_SECRET"] = item.secret.get_secret_value()
    for item in clients:
        prefix = f"LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_{item.ordinal}"
        values[f"{prefix}_ID"] = item.olm_id
        values[f"{prefix}_SECRET"] = item.secret.get_secret_value()
    return values


def parse_platform_secret_values(values: Mapping[str, str]) -> PangolinPlatformCredentials:
    encoded = values.get(PLATFORM_STATE_KEY)
    if encoded is None:
        return PangolinPlatformCredentials()
    try:
        return PangolinPlatformCredentials.model_validate_json(encoded)
    except ValidationError as exc:
        raise InvalidInputError("Pangolin platform credential state is unreadable") from exc


def _site_credential(
    credentials: PangolinPlatformCredentials,
    ordinal: int,
) -> PangolinPlatformSiteCredential | None:
    return next((item for item in credentials.sites if item.ordinal == ordinal), None)


def _client_credential(
    credentials: PangolinPlatformCredentials,
    ordinal: int,
) -> PangolinPlatformClientCredential | None:
    return next((item for item in credentials.clients if item.ordinal == ordinal), None)


def _require_unique_ordinals(credentials: PangolinPlatformCredentials) -> None:
    site_ordinals = [item.ordinal for item in credentials.sites]
    client_ordinals = [item.ordinal for item in credentials.clients]
    if len(site_ordinals) != len(set(site_ordinals)):
        raise InvalidInputError("Pangolin platform site credentials contain duplicate ordinals")
    if len(client_ordinals) != len(set(client_ordinals)):
        raise InvalidInputError("Pangolin platform client credentials contain duplicate ordinals")


__all__ = [
    "PLATFORM_CLIENT_RECORD_IDS_KEY",
    "PLATFORM_SITE_IDS_KEY",
    "PLATFORM_STATE_KEY",
    "PangolinPlatformBootstrap",
    "PangolinPlatformBootstrapResult",
    "PangolinPlatformClientCredential",
    "PangolinPlatformCredentialStore",
    "PangolinPlatformCredentials",
    "PangolinPlatformSiteCredential",
    "parse_platform_secret_values",
    "platform_secret_values",
]

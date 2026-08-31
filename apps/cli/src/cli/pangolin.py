from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, TypeGuard

import httpx
import typer
from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError
from lazycloud.cli.components.output import print_payload
from provider_pangolin import (
    PangolinPlatformBootstrap,
    PangolinPlatformBootstrapResult,
    PangolinPlatformCredentials,
    PangolinSettings,
    parse_platform_secret_values,
    platform_secret_values,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from shared.errors import InvalidInputError, UpstreamUnavailableError

pangolin_app = typer.Typer(help="Bootstrap the external Pangolin platform identities.")

_STRING_MAP = TypeAdapter(dict[str, str])
_LOCAL_ORGANIZATION_NAME = "LazyCloud local"
_LOCAL_ORGANIZATION_SUBNET = "100.90.128.0/20"
_LOCAL_ORGANIZATION_UTILITY_SUBNET = "100.96.128.0/20"
_LOCAL_SERVER_ADMIN_EMAIL = "admin@lazycloud.test"
_CSRF_TOKEN = "x-csrf-protection"
_PANGOLIN_PROVIDER_ACTIONS = (
    "createClient",
    "createOrg",
    "createOrgDomain",
    "createResource",
    "createSite",
    "createSiteResource",
    "createTarget",
    "deleteClient",
    "deleteOrgDomain",
    "deleteResource",
    "deleteSite",
    "deleteSiteResource",
    "getClient",
    "getDNSRecords",
    "getDomain",
    "getOrg",
    "getSite",
    "getTarget",
    "listClients",
    "listOrgDomains",
    "listResources",
    "listResourceUsers",
    "listSites",
    "listSiteResources",
    "listTargets",
    "setResourceUsers",
    "updateResource",
    "updateTarget",
)


class _PangolinEnvelope(BaseModel):
    data: JsonValue = None
    success: bool
    error: bool = False
    message: str = ""
    status: int

    model_config = ConfigDict(extra="ignore", frozen=True)


class _PangolinInitialSetup(BaseModel):
    complete: bool

    model_config = ConfigDict(extra="ignore", frozen=True)


class _PangolinLicenseStatus(BaseModel):
    valid: bool = Field(alias="isLicenseValid")
    tier: str | None = None

    model_config = ConfigDict(extra="ignore", frozen=True)


@dataclass(frozen=True, slots=True)
class _LocalPangolinServerBootstrap:
    endpoint: str
    internal_api_url: str
    api_key: SecretStr
    license_key: SecretStr
    server_secret: SecretStr
    setup_token_file: Path
    timeout_seconds: float

    @classmethod
    def from_environment(cls, settings: PangolinSettings) -> _LocalPangolinServerBootstrap:
        return cls(
            endpoint=settings.endpoint,
            internal_api_url=os.environ.get(
                "LAZYCLOUD_PANGOLIN_INTERNAL_API_URL",
                "http://pangolin:3001/api/v1",
            )
            .strip()
            .rstrip("/"),
            api_key=settings.api_key,
            license_key=SecretStr(_required_environment("LAZYCLOUD_PANGOLIN_LICENSE_KEY")),
            server_secret=SecretStr(_required_environment("LAZYCLOUD_PANGOLIN_SERVER_SECRET")),
            setup_token_file=Path(
                os.environ.get(
                    "LAZYCLOUD_PANGOLIN_SETUP_TOKEN_FILE",
                    "/var/lib/lazycloud/pangolin-bootstrap/setup-token",
                )
            ),
            timeout_seconds=settings.timeout_seconds,
        )

    def ensure_license(self) -> _PangolinLicenseStatus:
        status = self._license_status()
        with httpx.Client(
            base_url=f"{self.endpoint}/api/v1",
            headers={"Accept": "application/json", "X-CSRF-Token": _CSRF_TOKEN},
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            setup = _PangolinInitialSetup.model_validate(
                self._request(client, "GET", "/auth/initial-setup-complete")
            )
            password = self._admin_password()
            if not setup.complete:
                self._request(
                    client,
                    "PUT",
                    "/auth/set-server-admin",
                    json={
                        "email": _LOCAL_SERVER_ADMIN_EMAIL,
                        "password": password,
                        "setupToken": self._setup_token(),
                    },
                )
            self._request(
                client,
                "POST",
                "/auth/login",
                json={"email": _LOCAL_SERVER_ADMIN_EMAIL, "password": password},
            )
            if not status.valid:
                rechecked = _PangolinLicenseStatus.model_validate(
                    self._request(client, "POST", "/license/recheck")
                )
                if not rechecked.valid:
                    self._request(
                        client,
                        "POST",
                        "/license/activate",
                        json={"licenseKey": self.license_key.get_secret_value()},
                    )
            self._ensure_api_key_actions(client)
        status = self._license_status()
        if not status.valid:
            raise UpstreamUnavailableError("Pangolin Enterprise license activation did not persist")
        return status

    def _ensure_api_key_actions(self, client: httpx.Client) -> None:
        api_key_id, separator, api_key_secret = self.api_key.get_secret_value().partition(".")
        if (
            not separator
            or len(api_key_id) != 15
            or not api_key_id.isalnum()
            or api_key_id.lower() != api_key_id
            or not api_key_secret
        ):
            raise InvalidInputError("LAZYCLOUD_PANGOLIN_API_KEY is invalid")
        self._request(
            client,
            "POST",
            f"/api-key/{api_key_id}/actions",
            json={"actionIds": list(_PANGOLIN_PROVIDER_ACTIONS)},
        )

    def _license_status(self) -> _PangolinLicenseStatus:
        with httpx.Client(
            base_url=self.internal_api_url,
            headers={"Accept": "application/json"},
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            return _PangolinLicenseStatus.model_validate(
                self._request(client, "GET", "/license/status")
            )

    def _setup_token(self) -> str:
        try:
            token = self.setup_token_file.read_text().strip()
        except OSError as exc:
            raise UpstreamUnavailableError("Pangolin setup token is unavailable") from exc
        if len(token) != 32 or not token.isascii() or not token.isalnum() or token.lower() != token:
            raise InvalidInputError("Pangolin setup token is invalid")
        return token

    def _admin_password(self) -> str:
        digest = hmac.new(
            self.server_secret.get_secret_value().encode(),
            b"lazycloud-local-pangolin-admin",
            hashlib.sha256,
        ).hexdigest()
        return f"Lc!1{digest}"

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        path: str,
        *,
        json: dict[str, str | list[str]] | None = None,
    ) -> JsonValue:
        try:
            response = client.request(method, path, json=json)
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(
                f"Pangolin server bootstrap request failed for {method} {path}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise UpstreamUnavailableError(
                f"Pangolin server bootstrap rejected {method} {path} ({response.status_code})"
            )
        try:
            envelope = _PangolinEnvelope.model_validate_json(response.content)
        except ValidationError as exc:
            raise UpstreamUnavailableError(
                f"Pangolin server bootstrap returned unreadable data for {method} {path}"
            ) from exc
        if not envelope.success or envelope.error:
            raise UpstreamUnavailableError(f"Pangolin server bootstrap rejected {method} {path}")
        return envelope.data


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise InvalidInputError(f"{name} is required")
    return value


class Boto3ClientFactory(Protocol):
    def client(self, service_name: str) -> object: ...


class SecretsManagerClient(Protocol):
    def describe_secret(self, *, SecretId: str) -> dict[str, object]: ...

    def get_secret_value(self, *, SecretId: str) -> dict[str, object]: ...

    def put_secret_value(self, *, SecretId: str, SecretString: str) -> object: ...


def _is_boto3_client_factory(value: object) -> TypeGuard[Boto3ClientFactory]:
    return callable(getattr(value, "client", None))


def _is_secrets_manager_client(value: object) -> TypeGuard[SecretsManagerClient]:
    return all(
        callable(getattr(value, operation, None))
        for operation in ("describe_secret", "get_secret_value", "put_secret_value")
    )


class FilesystemPangolinCredentialStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self) -> PangolinPlatformCredentials:
        state = self.directory / "state.json"
        if not state.exists():
            return PangolinPlatformCredentials()
        return parse_platform_secret_values({"state.json": state.read_text()})

    def save(self, credentials: PangolinPlatformCredentials) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        temporary_files: dict[str, Path] = {}
        for name, value in platform_secret_values(credentials).items():
            destination = self.directory / name
            temporary = destination.with_name(f".{destination.name}.tmp")
            temporary.write_text(value)
            os.chmod(temporary, 0o600)
            temporary_files[name] = temporary
        for name in sorted(temporary_files, key=lambda item: item == "state.json"):
            os.replace(temporary_files[name], self.directory / name)


class AwsPangolinCredentialStore:
    def __init__(self, *, client: SecretsManagerClient, secret_id: str) -> None:
        self.client = client
        self.secret_id = secret_id

    def load(self) -> PangolinPlatformCredentials:
        try:
            response = self.client.get_secret_value(SecretId=self.secret_id)
            encoded = response.get("SecretString")
            if not isinstance(encoded, str):
                raise UpstreamUnavailableError(
                    f"Secrets Manager value {self.secret_id} is not a JSON string"
                )
            values = _STRING_MAP.validate_json(encoded)
        except ClientError as exc:
            if _client_error_code(exc) == "ResourceNotFoundException":
                try:
                    self.client.describe_secret(SecretId=self.secret_id)
                except (BotoCoreError, ClientError) as describe_exc:
                    raise UpstreamUnavailableError(
                        f"Secrets Manager value {self.secret_id} does not exist"
                    ) from describe_exc
                return PangolinPlatformCredentials()
            raise UpstreamUnavailableError(
                f"Secrets Manager value {self.secret_id} is unavailable or unreadable"
            ) from exc
        except (BotoCoreError, ValidationError) as exc:
            raise UpstreamUnavailableError(
                f"Secrets Manager value {self.secret_id} is unavailable or unreadable"
            ) from exc
        return parse_platform_secret_values(values)

    def save(self, credentials: PangolinPlatformCredentials) -> None:
        try:
            self.client.put_secret_value(
                SecretId=self.secret_id,
                SecretString=json.dumps(
                    platform_secret_values(credentials),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        except (BotoCoreError, ClientError) as exc:
            raise UpstreamUnavailableError(
                f"Secrets Manager value {self.secret_id} could not be persisted"
            ) from exc


def _secrets_manager_client(region: str) -> SecretsManagerClient:
    source: object = Session(region_name=region)
    if not _is_boto3_client_factory(source):
        raise RuntimeError("boto3 session lacks the client factory operation")
    candidate = source.client("secretsmanager")
    if not _is_secrets_manager_client(candidate):
        raise RuntimeError("boto3 Secrets Manager client lacks required operations")
    return candidate


def _client_error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    return str(error.get("Code", "")) if isinstance(error, Mapping) else ""


@pangolin_app.command("bootstrap-filesystem")
def bootstrap_filesystem(
    ctx: typer.Context,
    directory: Annotated[
        Path,
        typer.Option(
            "--directory",
            file_okay=False,
            resolve_path=True,
            help="Directory shared with the local Pangolin containers.",
        ),
    ],
    site_count: Annotated[int, typer.Option("--site-count", min=1)] = 1,
    client_count: Annotated[int, typer.Option("--client-count", min=1)] = 1,
    public_hostname: Annotated[str, typer.Option("--public-hostname", min=1)] = (
        "lazycloud.localhost"
    ),
) -> None:
    settings = PangolinSettings()
    _LocalPangolinServerBootstrap.from_environment(settings).ensure_license()
    client = settings.client()
    client.ensure_organization(
        name=_LOCAL_ORGANIZATION_NAME,
        subnet=_LOCAL_ORGANIZATION_SUBNET,
        utility_subnet=_LOCAL_ORGANIZATION_UTILITY_SUBNET,
    )
    result = PangolinPlatformBootstrap(
        client,
        FilesystemPangolinCredentialStore(directory),
    ).ensure(
        site_count=site_count,
        client_count=client_count,
        public_hostname=public_hostname,
        public_tls=False,
    )
    print_payload(ctx, _result_payload(result))


@pangolin_app.command("bootstrap-aws")
def bootstrap_aws(
    ctx: typer.Context,
    secret_id: Annotated[str, typer.Option("--secret-id", min=1)],
    region: Annotated[str, typer.Option("--region", min=1)],
    public_hostname: Annotated[str, typer.Option("--public-hostname", min=1)],
    site_count: Annotated[int, typer.Option("--site-count", min=1)] = 2,
    client_count: Annotated[int, typer.Option("--client-count", min=1)] = 2,
) -> None:
    result = PangolinPlatformBootstrap(
        PangolinSettings().client(),
        AwsPangolinCredentialStore(
            client=_secrets_manager_client(region),
            secret_id=secret_id,
        ),
    ).ensure(
        site_count=site_count,
        client_count=client_count,
        public_hostname=public_hostname,
        public_tls=True,
    )
    print_payload(ctx, _result_payload(result))


def _result_payload(result: PangolinPlatformBootstrapResult) -> dict[str, int]:
    return {
        "sites": result.site_count,
        "clients": result.client_count,
        "created_sites": result.created_site_count,
        "created_clients": result.created_client_count,
    }


__all__ = ["pangolin_app"]

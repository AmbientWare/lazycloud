from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Protocol, TypeGuard

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
from pydantic import TypeAdapter, ValidationError
from shared.errors import UpstreamUnavailableError

pangolin_app = typer.Typer(help="Bootstrap the external Pangolin platform identities.")

_STRING_MAP = TypeAdapter(dict[str, str])


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
    result = PangolinPlatformBootstrap(
        PangolinSettings().client(),
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

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, TypeGuard

import typer
from boto3.session import Session
from botocore.exceptions import ClientError
from networking.wireguard_keys import ensure_wireguard_key_document
from provider_aws.boto3_clients import has_operations, is_boto3_client_factory
from pydantic import TypeAdapter, ValidationError

wireguard_app = typer.Typer(help="Operate the WireGuard deployment gateway.")
_JSON_OBJECT = TypeAdapter(dict[str, object])


class _SecretsManagerClient(Protocol):
    def describe_secret(self, *, SecretId: str) -> Mapping[str, object]: ...

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, object]: ...

    def put_secret_value(
        self,
        *,
        SecretId: str,
        SecretString: str,
    ) -> Mapping[str, object]: ...


def _is_secrets_manager_client(value: object) -> TypeGuard[_SecretsManagerClient]:
    return has_operations(value, ("describe_secret", "get_secret_value", "put_secret_value"))


@wireguard_app.command("bootstrap-aws")
def bootstrap_aws(
    secret_id: str = typer.Option(..., help="Secrets Manager key document name or ARN."),
    region: str = typer.Option(..., help="AWS region holding the key document."),
    platform_peers: int = typer.Option(2, min=1, max=32),
) -> None:
    source: object = Session(region_name=region)
    if not is_boto3_client_factory(source):
        raise RuntimeError("boto3 session lacks the client factory operation")
    client = source.client("secretsmanager")
    if not _is_secrets_manager_client(client):
        raise RuntimeError("boto3 Secrets Manager client lacks required operations")
    client.describe_secret(SecretId=secret_id)
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        existing: dict[str, object] = {}
    else:
        secret_string = response.get("SecretString")
        if not isinstance(secret_string, str):
            raise RuntimeError("WireGuard key document must be a JSON string secret")
        try:
            existing = _JSON_OBJECT.validate_json(secret_string)
        except ValidationError as exc:
            raise RuntimeError("WireGuard key document must be a JSON object") from exc
    values = ensure_wireguard_key_document(existing, platform_peers=platform_peers)
    if values != existing:
        client.put_secret_value(
            SecretId=secret_id,
            SecretString=json.dumps(values, sort_keys=True),
        )
    typer.echo("WireGuard deployment keys are ready")

"""Initialize one deployment's tunnel issuer without rotating existing credentials."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cryptography import x509
from cryptography.x509.oid import NameOID
from identity.tunnel_certificates import (
    TunnelCertificateIssuer,
    create_tunnel_certificate_authority,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

_CERTIFICATE = "LAZYCLOUD_TUNNEL_ISSUER_CERTIFICATE_PEM"
_KEY = "LAZYCLOUD_TUNNEL_ISSUER_PRIVATE_KEY_PEM"
_BOOTSTRAP = "LAZYCLOUD_TUNNEL_GATEWAY_BOOTSTRAP_SECRET"
_DOCUMENT = TypeAdapter(dict[str, JsonValue], config=ConfigDict(hide_input_in_errors=True))


class SecretVersion(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    SecretString: str = Field(repr=False)
    VersionId: str


def _write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _validate(directory: Path, hostname: str) -> None:
    certificate = directory / "certificate.pem"
    TunnelCertificateIssuer.load(certificate, directory / "private-key.pem")
    authority = x509.load_pem_x509_certificate(certificate.read_bytes())
    names = authority.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(names) != 1 or names[0].value != hostname:
        raise ValueError("Existing tunnel issuer belongs to another deployment hostname")


def initialize_local(directory: Path, hostname: str) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    certificate = directory / "certificate.pem"
    key = directory / "private-key.pem"
    if certificate.exists() or key.exists():
        _validate(directory, hostname)
        return
    authority = create_tunnel_certificate_authority(deployment_hostname=hostname)
    _write(key, authority.private_key_pem)
    _write(certificate, authority.certificate_pem)
    _validate(directory, hostname)


def initialize_aws(secret_id: str, region: str, hostname: str) -> None:
    command = ["aws", "--profile", "default", "--region", region, "secretsmanager"]
    current = SecretVersion.model_validate_json(
        subprocess.check_output(
            [
                *command,
                "get-secret-value",
                "--secret-id",
                secret_id,
                "--version-stage",
                "AWSCURRENT",
            ]
        )
    )
    values = _DOCUMENT.validate_json(current.SecretString)
    present = {_CERTIFICATE, _KEY, _BOOTSTRAP}.intersection(values)
    if present and len(present) != 3:
        raise ValueError(
            "Operator secret contains an incomplete tunnel identity; refusing rotation"
        )
    with TemporaryDirectory(prefix="lazycloud-tunnel-issuer-") as temporary:
        directory = Path(temporary)
        if present:
            certificate, key, bootstrap = values[_CERTIFICATE], values[_KEY], values[_BOOTSTRAP]
            if not isinstance(certificate, str) or not isinstance(key, str):
                raise ValueError("Tunnel issuer PEM properties must be strings")
            if not isinstance(bootstrap, str) or len(bootstrap) < 32:
                raise ValueError("Gateway bootstrap credential must contain at least 32 characters")
            _write(directory / "certificate.pem", certificate)
            _write(directory / "private-key.pem", key)
            _validate(directory, hostname)
            return
        initialize_local(directory, hostname)
        values.update(
            {
                _CERTIFICATE: (directory / "certificate.pem").read_text(),
                _KEY: (directory / "private-key.pem").read_text(),
                _BOOTSTRAP: secrets.token_urlsafe(48),
            }
        )
        document = directory / "operator.json"
        _write(document, json.dumps(values))
        version = str(uuid4())
        stage = f"lazycloud-tunnel-bootstrap-{version}"
        subprocess.run(
            [
                *command,
                "put-secret-value",
                "--secret-id",
                secret_id,
                "--client-request-token",
                version,
                "--version-stages",
                stage,
                "--secret-string",
                f"file://{document}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            subprocess.run(
                [
                    *command,
                    "update-secret-version-stage",
                    "--secret-id",
                    secret_id,
                    "--version-stage",
                    "AWSCURRENT",
                    "--move-to-version-id",
                    version,
                    "--remove-from-version-id",
                    current.VersionId,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        finally:
            subprocess.run(
                [
                    *command,
                    "update-secret-version-stage",
                    "--secret-id",
                    secret_id,
                    "--version-stage",
                    stage,
                    "--remove-from-version-id",
                    version,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_subparsers(dest="target", required=True)
    local = targets.add_parser("local")
    local.add_argument("--directory", type=Path, required=True)
    local.add_argument("--hostname", required=True)
    aws = targets.add_parser("aws")
    aws.add_argument("--secret-id", required=True)
    aws.add_argument("--region", required=True)
    aws.add_argument("--hostname", required=True)
    args = parser.parse_args()
    if args.target == "local":
        initialize_local(args.directory, args.hostname)
    else:
        initialize_aws(args.secret_id, args.region, args.hostname)
    print("Tunnel issuer initialized; existing credentials preserved.")


if __name__ == "__main__":
    main()

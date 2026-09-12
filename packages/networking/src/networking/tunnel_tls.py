from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.verification import PolicyBuilder, Store
from pydantic import BaseModel, Field


class _CertificateBundle(BaseModel):
    certificate_pem: str = Field(repr=False)
    trust_bundle_pem: str = Field(repr=False)


@dataclass(frozen=True, slots=True)
class TunnelClientCredentials:
    certificate: x509.Certificate
    credentials: grpc.ChannelCredentials


@dataclass(frozen=True, slots=True)
class TunnelCredentials:
    key_path: Path = field(repr=False)
    bundle_path: Path = field(repr=False)

    @property
    def certificate_pem(self) -> str:
        return self._bundle().certificate_pem

    def install(self, *, certificate_pem: str, trust_bundle_pem: str) -> None:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        roots = x509.load_pem_x509_certificates(trust_bundle_pem.encode("ascii"))
        PolicyBuilder().store(Store(roots)).build_client_verifier().verify(certificate, [])
        key = _private_key(self.key_path)
        encoding = serialization.Encoding.DER
        form = serialization.PublicFormat.SubjectPublicKeyInfo
        if certificate.public_key().public_bytes(encoding, form) != key.public_key().public_bytes(
            encoding, form
        ):
            raise ValueError("Tunnel certificate does not match the locally generated key")
        bundle = _CertificateBundle(
            certificate_pem=certificate_pem, trust_bundle_pem=trust_bundle_pem
        )
        self.bundle_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.bundle_path.parent, prefix=".certificate-"
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(bundle.model_dump_json())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.bundle_path)
            _sync_directory(self.bundle_path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def client(self) -> TunnelClientCredentials:
        self._validate_key()
        bundle = self._bundle()
        return TunnelClientCredentials(
            certificate=x509.load_pem_x509_certificate(bundle.certificate_pem.encode("ascii")),
            credentials=grpc.ssl_channel_credentials(
                bundle.trust_bundle_pem.encode("ascii"),
                self.key_path.read_bytes(),
                bundle.certificate_pem.encode("ascii"),
            ),
        )

    def server(self) -> grpc.ServerCredentials:
        return grpc.dynamic_ssl_server_credentials(
            self._server_configuration(),
            self._server_configuration,
            require_client_authentication=True,
        )

    def _server_configuration(self) -> grpc.ServerCertificateConfiguration:
        self._validate_key()
        bundle = self._bundle()
        return grpc.ssl_server_certificate_configuration(
            [(self.key_path.read_bytes(), bundle.certificate_pem.encode("ascii"))],
            root_certificates=bundle.trust_bundle_pem.encode("ascii"),
        )

    def _bundle(self) -> _CertificateBundle:
        return _CertificateBundle.model_validate_json(self.bundle_path.read_bytes())

    def _validate_key(self) -> None:
        if self.key_path.stat().st_mode & 0o077:
            raise ValueError("Tunnel private key must be readable only by its owner")


def agent_certificate_request(key_path: Path) -> str:
    key = _private_key(key_path)
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, hashes.SHA256())
    )
    return request.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _private_key(key_path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        contents = key_path.read_bytes()
    except FileNotFoundError:
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = ec.generate_private_key(ec.SECP256R1())
        descriptor, temporary = tempfile.mkstemp(dir=key_path.parent, prefix=".key-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, key_path)
                _sync_directory(key_path.parent)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
        contents = key_path.read_bytes()
    if key_path.stat().st_mode & 0o077:
        raise ValueError("Tunnel private key must be readable only by its owner")
    loaded = serialization.load_pem_private_key(contents, password=None)
    if not isinstance(loaded, ec.EllipticCurvePrivateKey) or not isinstance(
        loaded.curve, ec.SECP256R1
    ):
        raise ValueError("Tunnel private key must use P-256")
    return loaded


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

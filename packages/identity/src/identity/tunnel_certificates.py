from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.types import CertificatePublicKeyTypes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from shared.errors import InvalidInputError
from shared.http.agent_identity import (
    AgentCertificate,
    AgentTunnelIdentity,
    ServiceCertificateResponse,
    ServiceTunnelIdentity,
    TunnelServiceRole,
)

TUNNEL_CERTIFICATE_LIFETIME = timedelta(hours=1)
_CLOCK_SKEW = timedelta(minutes=1)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True, slots=True)
class TunnelCertificateAuthority:
    certificate_pem: str = field(repr=False)
    private_key_pem: str = field(repr=False)


def create_tunnel_certificate_authority(*, deployment_hostname: str) -> TunnelCertificateAuthority:
    hostname = _dns_name(deployment_hostname)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC).replace(microsecond=0)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return TunnelCertificateAuthority(
        certificate_pem=_certificate_pem(certificate),
        private_key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii"),
    )


@dataclass(frozen=True, slots=True)
class TunnelCertificateIssuer:
    _certificate: x509.Certificate = field(repr=False)
    _private_key: ec.EllipticCurvePrivateKey = field(repr=False)

    @classmethod
    def load(cls, certificate_path: Path, private_key_path: Path) -> TunnelCertificateIssuer:
        with private_key_path.open("rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_uid not in {0, os.geteuid()}
            ):
                raise RuntimeError("Tunnel issuer private key requires an owner-only regular file")
            key_pem = source.read(16_385)
            if len(key_pem) > 16_384:
                raise RuntimeError("Tunnel issuer private key exceeds its accepted size")
            key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise RuntimeError("Tunnel issuer requires a P-256 private key")
        certificates = x509.load_pem_x509_certificates(certificate_path.read_bytes())
        if len(certificates) != 1:
            raise RuntimeError("Tunnel issuer requires exactly one CA certificate")
        certificate = certificates[0]
        if _public_key_digest(key.public_key()) != _public_key_digest(certificate.public_key()):
            raise RuntimeError("Tunnel issuer certificate and private key do not match")
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
        if (
            not constraints.critical
            or not constraints.value.ca
            or constraints.value.path_length != 0
            or not usage.critical
            or not usage.value.key_cert_sign
        ):
            raise RuntimeError("Tunnel issuer certificate must authorize only direct leaf issuance")
        try:
            certificate.verify_directly_issued_by(certificate)
        except (ValueError, InvalidSignature) as exc:
            raise RuntimeError("Tunnel issuer CA must have a valid self-signature") from exc
        issuer = cls(certificate, key)
        issuer._validity()
        return issuer

    @property
    def trust_bundle_pem(self) -> str:
        return _certificate_pem(self._certificate)

    def issue_agent(self, csr_pem: str, identity: AgentTunnelIdentity) -> AgentCertificate:
        certificate = self._issue(
            csr_pem,
            identity_uri=identity.identity_uri,
            subject="LazyCloud agent",
            dns_names=(),
            server=False,
        )
        return AgentCertificate(
            identity=identity,
            certificate_pem=_certificate_pem(certificate),
            trust_bundle_pem=self.trust_bundle_pem,
            not_before=certificate.not_valid_before_utc,
            expires_at=certificate.not_valid_after_utc,
        )

    def issue_service(
        self,
        csr_pem: str,
        role: TunnelServiceRole,
        instance_id: str,
        dns_names: tuple[str, ...] = (),
    ) -> ServiceCertificateResponse:
        identity = ServiceTunnelIdentity(role=role, instance_id=instance_id)
        server = identity.role is TunnelServiceRole.Gateway
        if server != bool(dns_names):
            raise InvalidInputError("Only gateway service certificates require DNS names")
        certificate = self._issue(
            csr_pem,
            identity_uri=identity.identity_uri,
            subject=f"LazyCloud {role}",
            dns_names=dns_names,
            server=server,
        )
        return ServiceCertificateResponse(
            identity=identity,
            certificate_pem=_certificate_pem(certificate),
            trust_bundle_pem=self.trust_bundle_pem,
            not_before=certificate.not_valid_before_utc,
            expires_at=certificate.not_valid_after_utc,
        )

    def _validity(self) -> tuple[datetime, datetime]:
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = now + TUNNEL_CERTIFICATE_LIFETIME
        if now < self._certificate.not_valid_before_utc or (
            expires_at > self._certificate.not_valid_after_utc
        ):
            raise RuntimeError("Tunnel issuer CA cannot cover the next certificate lifetime")
        return max(now - _CLOCK_SKEW, self._certificate.not_valid_before_utc), expires_at

    def _issue(
        self,
        csr_pem: str,
        *,
        identity_uri: str,
        subject: str,
        dns_names: tuple[str, ...],
        server: bool,
    ) -> x509.Certificate:
        key = _csr_public_key(csr_pem)
        names = tuple(_dns_name(name) for name in dns_names)
        if len(names) != len(set(names)):
            raise InvalidInputError("Tunnel certificate DNS names must be unique")
        not_before, expires_at = self._validity()
        eku = [ExtendedKeyUsageOID.CLIENT_AUTH]
        if server:
            eku.append(ExtendedKeyUsageOID.SERVER_AUTH)
        return (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(self._certificate.subject)
            .public_key(key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(expires_at)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_key_usage(ca=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage(eku), critical=False)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(identity_uri), *map(x509.DNSName, names)]
                ),
                critical=False,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._private_key.public_key()),
                critical=False,
            )
            .sign(self._private_key, hashes.SHA256())
        )


def csr_public_key_sha256(csr_pem: str) -> str:
    return _public_key_digest(_csr_public_key(csr_pem))


def certificate_public_key_sha256(certificate_pem: str) -> str:
    return _public_key_digest(_load_leaf(certificate_pem).public_key())


def agent_identity_from_verified_certificate(certificate_pem: str) -> AgentTunnelIdentity:
    """Read identity after TLS has verified trust, validity, and possession of the key."""
    certificate = _load_leaf(certificate_pem)
    identity = AgentTunnelIdentity.from_uri(_identity_uri(certificate))
    sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if len(sans) != 1:
        raise ValueError("Agent certificates must contain only their agent identity SAN")
    _require_eku(certificate, server=False)
    return identity


def service_identity_from_verified_certificate(certificate_pem: str) -> ServiceTunnelIdentity:
    """Read identity after TLS has verified trust, validity, and possession of the key."""
    certificate = _load_leaf(certificate_pem)
    identity = ServiceTunnelIdentity.from_uri(_identity_uri(certificate))
    _require_eku(certificate, server=identity.role is TunnelServiceRole.Gateway)
    return identity


def _load_leaf(certificate_pem: str) -> x509.Certificate:
    certificates = x509.load_pem_x509_certificates(certificate_pem.encode("ascii"))
    if len(certificates) != 1:
        raise ValueError("Tunnel peer must supply exactly one leaf certificate")
    certificate = certificates[0]
    if certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("A tunnel peer must not present a CA certificate")
    return certificate


def _identity_uri(certificate: x509.Certificate) -> str:
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = names.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise ValueError("Tunnel certificates require exactly one identity URI")
    return uris[0]


def _require_eku(certificate: x509.Certificate, *, server: bool) -> None:
    actual = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    expected = {ExtendedKeyUsageOID.CLIENT_AUTH}
    if server:
        expected.add(ExtendedKeyUsageOID.SERVER_AUTH)
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError("Tunnel certificate usage does not match its identity role")


def _csr_public_key(csr_pem: str) -> ec.EllipticCurvePublicKey:
    if not 0 < len(csr_pem) <= 16_384:
        raise InvalidInputError("Tunnel certificate request is outside the accepted size")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        key = csr.public_key()
        if not csr.is_signature_valid or not isinstance(
            csr.signature_hash_algorithm, hashes.SHA256
        ):
            raise ValueError("Invalid request signature")
        return _p256_public_key(key)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise InvalidInputError("Tunnel certificate request requires a signed P-256 CSR") from exc


def _p256_public_key(key: CertificatePublicKeyTypes) -> ec.EllipticCurvePublicKey:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Tunnel certificates require a P-256 public key")
    return key


def _public_key_digest(key: CertificatePublicKeyTypes) -> str:
    return hashlib.sha256(
        _p256_public_key(key).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()


def _key_usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not ca,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def _certificate_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _dns_name(value: str) -> str:
    if len(value) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in value.split(".")):
        raise InvalidInputError("Tunnel certificate requires a canonical DNS hostname")
    return value


__all__ = [
    "TUNNEL_CERTIFICATE_LIFETIME",
    "TunnelCertificateAuthority",
    "TunnelCertificateIssuer",
    "agent_identity_from_verified_certificate",
    "certificate_public_key_sha256",
    "create_tunnel_certificate_authority",
    "csr_public_key_sha256",
    "service_identity_from_verified_certificate",
]

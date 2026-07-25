from __future__ import annotations

import argparse
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def ensure_local_tcp_certificate(
    *,
    certificate_file: Path,
    key_file: Path,
    external_host: str,
    validity_days: int = 30,
    renew_before_days: int = 2,
) -> bool:
    domain = external_host.strip(".").lower()
    if not domain:
        raise ValueError("TCP ingress external host is required")
    if _certificate_is_current(
        certificate_file,
        key_file,
        domain=domain,
        renew_before=timedelta(days=max(renew_before_days, 0)),
    ):
        return False
    now = datetime.now(UTC)
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"*.{domain}")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=max(validity_days, 1)))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(f"*.{domain}"), x509.DNSName(domain)]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    key_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(key_file, key_bytes, mode=0o600)
    _atomic_write(certificate_file, certificate_bytes, mode=0o644)
    return True


def _certificate_is_current(
    certificate_file: Path,
    key_file: Path,
    *,
    domain: str,
    renew_before: timedelta,
) -> bool:
    try:
        certificate = x509.load_pem_x509_certificate(certificate_file.read_bytes())
        private_key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        certificate_key = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    return (
        f"*.{domain}" in names
        and public_key == certificate_key
        and certificate.not_valid_after_utc > datetime.now(UTC) + renew_before
    )


def _atomic_write(path: Path, value: bytes, *, mode: int) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lazycloud-tcp-certificate")
    parser.add_argument("--certificate-file", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--external-host", required=True)
    parser.add_argument("--validity-days", type=int, default=30)
    parser.add_argument("--renew-before-days", type=int, default=2)
    return parser


class TcpCertificateArguments(argparse.Namespace):
    certificate_file: Path
    key_file: Path
    external_host: str
    validity_days: int
    renew_before_days: int


def main(argv: list[str] | None = None) -> None:
    args = TcpCertificateArguments()
    build_parser().parse_args(argv, namespace=args)
    changed = ensure_local_tcp_certificate(
        certificate_file=args.certificate_file,
        key_file=args.key_file,
        external_host=args.external_host,
        validity_days=args.validity_days,
        renew_before_days=args.renew_before_days,
    )
    print("created" if changed else "current")


if __name__ == "__main__":
    main()


__all__ = ["ensure_local_tcp_certificate", "main"]

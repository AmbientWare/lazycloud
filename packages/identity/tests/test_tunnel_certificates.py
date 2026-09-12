from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from identity.tunnel_certificates import (
    TunnelCertificateIssuer,
    agent_identity_from_verified_certificate,
    certificate_public_key_sha256,
    create_tunnel_certificate_authority,
    csr_public_key_sha256,
    service_identity_from_verified_certificate,
)
from shared.errors import InvalidInputError
from shared.http.agent_identity import AgentTunnelIdentity, TunnelServiceRole


@pytest.fixture
def issuer(tmp_path: Path) -> TunnelCertificateIssuer:
    authority = create_tunnel_certificate_authority(deployment_hostname="tunnel.example.test")
    certificate_path = tmp_path / "issuer.pem"
    key_path = tmp_path / "issuer-key.pem"
    certificate_path.write_text(authority.certificate_pem)
    key_path.write_text(authority.private_key_pem)
    key_path.chmod(0o600)
    return TunnelCertificateIssuer.load(certificate_path, key_path)


def _identity() -> AgentTunnelIdentity:
    return AgentTunnelIdentity(
        workspace_id=str(uuid4()), enrollment_id=str(uuid4()), credential_generation=2
    )


def _csr() -> x509.CertificateSigningRequest:
    key = ec.generate_private_key(ec.SECP256R1())
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "requested administrator")])
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=5), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(_identity().identity_uri),
                    x509.DNSName("unauthorized.example.test"),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )


def _csr_pem(csr: x509.CertificateSigningRequest) -> str:
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _policy(issuer: TunnelCertificateIssuer) -> PolicyBuilder:
    root = x509.load_pem_x509_certificate(issuer.trust_bundle_pem.encode("ascii"))
    return PolicyBuilder().store(Store([root]))


def test_issuance_uses_authorized_identity_and_only_the_verified_csr_key(
    issuer: TunnelCertificateIssuer,
) -> None:
    csr = _csr()
    identity = _identity()
    response = issuer.issue_agent(_csr_pem(csr), identity)
    certificate = x509.load_pem_x509_certificate(response.certificate_pem.encode("ascii"))
    _policy(issuer).build_client_verifier().verify(certificate, [])

    assert response.identity == identity
    assert agent_identity_from_verified_certificate(response.certificate_pem) == identity
    assert certificate.subject != csr.subject
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert certificate_public_key_sha256(response.certificate_pem) == csr_public_key_sha256(
        _csr_pem(csr)
    )
    assert csr_public_key_sha256(_csr_pem(_csr())) != csr_public_key_sha256(_csr_pem(csr))
    assert response.not_before <= datetime.now(UTC) < response.expires_at
    assert response.expires_at - response.not_before <= timedelta(hours=1, minutes=1)


def test_expiry_trust_and_service_roles_are_enforced(issuer: TunnelCertificateIssuer) -> None:
    agent = issuer.issue_agent(_csr_pem(_csr()), _identity())
    gateway = issuer.issue_service(
        _csr_pem(_csr()), TunnelServiceRole.Gateway, str(uuid4()), ("tunnel.example.test",)
    )
    control_plane = issuer.issue_service(
        _csr_pem(_csr()), TunnelServiceRole.ControlPlane, str(uuid4())
    )
    agent_leaf = x509.load_pem_x509_certificate(agent.certificate_pem.encode("ascii"))
    gateway_leaf = x509.load_pem_x509_certificate(gateway.certificate_pem.encode("ascii"))
    control_plane_leaf = x509.load_pem_x509_certificate(
        control_plane.certificate_pem.encode("ascii")
    )
    _policy(issuer).build_server_verifier(x509.DNSName("tunnel.example.test")).verify(
        gateway_leaf, []
    )
    _policy(issuer).build_client_verifier().verify(control_plane_leaf, [])
    assert service_identity_from_verified_certificate(gateway.certificate_pem) == gateway.identity
    assert (
        service_identity_from_verified_certificate(control_plane.certificate_pem)
        == control_plane.identity
    )
    for certificate in (agent_leaf, control_plane_leaf):
        with pytest.raises(VerificationError):
            _policy(issuer).build_server_verifier(x509.DNSName("tunnel.example.test")).verify(
                certificate, []
            )
    for certificate_pem in (gateway.certificate_pem, control_plane.certificate_pem):
        with pytest.raises(ValueError):
            agent_identity_from_verified_certificate(certificate_pem)
    with pytest.raises(ValueError):
        service_identity_from_verified_certificate(agent.certificate_pem)
    with pytest.raises(VerificationError):
        _policy(issuer).time(
            agent.expires_at + timedelta(seconds=1)
        ).build_client_verifier().verify(agent_leaf, [])
    unrelated_ca = create_tunnel_certificate_authority(deployment_hostname="other.example.test")
    unrelated_root = x509.load_pem_x509_certificate(unrelated_ca.certificate_pem.encode("ascii"))
    with pytest.raises(VerificationError):
        PolicyBuilder().store(Store([unrelated_root])).build_client_verifier().verify(
            agent_leaf, []
        )


def test_csr_signature_must_prove_key_possession(issuer: TunnelCertificateIssuer) -> None:
    encoded = _csr().public_bytes(serialization.Encoding.DER)
    altered = x509.load_der_x509_csr(encoded[:-1] + bytes([encoded[-1] ^ 1]))
    assert not altered.is_signature_valid
    with pytest.raises(InvalidInputError, match="signed P-256 CSR"):
        issuer.issue_agent(_csr_pem(altered), _identity())
    with pytest.raises(InvalidInputError, match="signed P-256 CSR"):
        csr_public_key_sha256(_csr_pem(altered))


def test_issuer_rejects_exposed_or_mismatched_private_key(
    issuer: TunnelCertificateIssuer, tmp_path: Path
) -> None:
    key_path = tmp_path / "issuer-key.pem"
    key_path.chmod(0o644)
    with pytest.raises(RuntimeError, match="owner-only"):
        TunnelCertificateIssuer.load(tmp_path / "issuer.pem", key_path)
    key_path.chmod(0o600)
    other = create_tunnel_certificate_authority(deployment_hostname="other.example.test")
    key_path.write_text(other.private_key_pem)
    with pytest.raises(RuntimeError, match="do not match"):
        TunnelCertificateIssuer.load(tmp_path / "issuer.pem", key_path)
    assert issuer.trust_bundle_pem


def test_trusted_ambiguous_or_noncanonical_identity_is_rejected(
    issuer: TunnelCertificateIssuer, tmp_path: Path
) -> None:
    identity = _identity()
    original = issuer.issue_agent(_csr_pem(_csr()), identity)
    certificate = x509.load_pem_x509_certificate(original.certificate_pem.encode("ascii"))
    key = serialization.load_pem_private_key(
        (tmp_path / "issuer-key.pem").read_bytes(), password=None
    )
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    for uris in (
        (identity.identity_uri, _identity().identity_uri),
        (identity.identity_uri + "/extra",),
        (identity.identity_uri.rsplit("/", maxsplit=1)[0] + "/02",),
    ):
        builder = (
            x509.CertificateBuilder()
            .subject_name(certificate.subject)
            .issuer_name(certificate.issuer)
            .public_key(certificate.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(certificate.not_valid_before_utc)
            .not_valid_after(certificate.not_valid_after_utc)
        )
        for extension in certificate.extensions:
            value = (
                x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri) for uri in uris])
                if isinstance(extension.value, x509.SubjectAlternativeName)
                else extension.value
            )
            builder = builder.add_extension(value, critical=extension.critical)
        changed = builder.sign(key, hashes.SHA256())
        _policy(issuer).build_client_verifier().verify(changed, [])
        with pytest.raises(ValueError):
            agent_identity_from_verified_certificate(
                changed.public_bytes(serialization.Encoding.PEM).decode("ascii")
            )

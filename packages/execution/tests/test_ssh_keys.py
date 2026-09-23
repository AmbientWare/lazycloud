from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    SSHCertificate,
    SSHCertificateType,
    load_ssh_public_identity,
)
from execution.ssh.keys import (
    openssh_public_key,
    pod_host_key,
    sign_user_certificate,
    workspace_user_authority,
)
from shared.errors import InvalidInputError
from shared.ssh import SSH_CERTIFICATE_PRINCIPAL, SSH_CERTIFICATE_TTL_SECONDS


class _PublicKey(Protocol):
    def public_bytes(self, encoding: Encoding, format: PublicFormat) -> bytes: ...


def _openssh(public_key: _PublicKey) -> str:
    return public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()


def test_user_certificate_is_signed_by_the_workspace_authority_for_root_only() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    user = Ed25519PrivateKey.generate()
    authority = workspace_user_authority("workspace-secret")

    signed = sign_user_certificate(
        authority,
        public_key=_openssh(user.public_key()) + " laptop",
        key_id="workspace=ws-1 holder=user:u-1",
        now=now,
    )

    certificate = load_ssh_public_identity(signed.certificate.encode())
    assert isinstance(certificate, SSHCertificate)
    certificate.verify_cert_signature()
    assert _openssh(certificate.signature_key()) == _openssh(
        workspace_user_authority("workspace-secret").public_key()
    )
    assert _openssh(certificate.public_key()) == _openssh(user.public_key())
    assert certificate.type is SSHCertificateType.USER
    assert certificate.valid_principals == [SSH_CERTIFICATE_PRINCIPAL.encode()]
    assert certificate.critical_options == {}
    assert certificate.valid_before == int(now.timestamp()) + SSH_CERTIFICATE_TTL_SECONDS
    assert int(now.timestamp()) - 600 < certificate.valid_after <= int(now.timestamp())
    assert signed.expires_at == datetime.fromtimestamp(certificate.valid_before, tz=UTC)
    assert certificate.key_id == b"workspace=ws-1 holder=user:u-1"
    assert _openssh(certificate.signature_key()) != _openssh(
        workspace_user_authority("another-workspace-secret").public_key()
    )


def test_user_certificate_refuses_keys_other_than_ed25519() -> None:
    rsa = generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidInputError, match="ssh-ed25519"):
        sign_user_certificate(
            workspace_user_authority("workspace-secret"),
            public_key=_openssh(rsa.public_key()),
            key_id="k",
            now=datetime(2026, 9, 23, tzinfo=UTC),
        )


def test_pod_host_key_is_stable_per_pod_and_distinct_across_pods() -> None:
    first = pod_host_key("signing", app_id="app-1", pod_name="box")
    again = pod_host_key("signing", app_id="app-1", pod_name="box")
    assert openssh_public_key(first, comment="c") == openssh_public_key(again, comment="c")
    for other in (
        pod_host_key("signing", app_id="app-2", pod_name="box"),
        pod_host_key("signing", app_id="app-1", pod_name="other"),
        pod_host_key("rotated", app_id="app-1", pod_name="box"),
    ):
        assert openssh_public_key(other, comment="c") != openssh_public_key(first, comment="c")

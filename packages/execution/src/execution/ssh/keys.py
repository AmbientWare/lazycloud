"""SSH identities derived from a workspace's credential secret.

Nothing here is stored. The workspace user certificate authority and every pod's
host key are ed25519 keys whose seeds are an HMAC of the credential secret under
a label naming what the key is for, so the control plane can recompute either one
whenever it signs a certificate or hands a container its identity. The secret is
never served, unlike the signing key members use to verify callbacks: a seed
anyone can read would let them mint certificates.

A host key is keyed on the app and the pod's name rather than on a stub or a
container: both change on every redeploy, and a host key that changed with them
would make each redeploy look like an impersonation to the client that pinned it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    SSHCertificateBuilder,
    SSHCertificateType,
    load_ssh_public_key,
)
from shared.errors import InvalidInputError
from shared.ssh import SSH_CERTIFICATE_PRINCIPAL, SSH_CERTIFICATE_TTL_SECONDS

_USER_CA_LABEL = "lazycloud-ssh-user-ca-v1"
_HOST_KEY_LABEL = "lazycloud-ssh-host-key-v1"
# Accepts a certificate from a client whose clock runs slightly behind ours.
_CERTIFICATE_BACKDATE = timedelta(minutes=5)
_CERTIFICATE_EXTENSIONS = (b"permit-port-forwarding", b"permit-pty")


@dataclass(frozen=True, slots=True)
class SignedUserCertificate:
    certificate: str
    expires_at: datetime


def workspace_user_authority(secret: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_seed(secret, _USER_CA_LABEL))


def pod_host_key(secret: str, *, app_id: str, pod_name: str) -> Ed25519PrivateKey:
    if not app_id or not pod_name:
        raise ValueError("a pod host key needs the app id and the pod name")
    return Ed25519PrivateKey.from_private_bytes(_seed(secret, _HOST_KEY_LABEL, app_id, pod_name))


def openssh_public_key(key: Ed25519PrivateKey, *, comment: str) -> str:
    line = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    return f"{line} {comment}"


def openssh_private_key(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode("ascii")


def sign_user_certificate(
    authority: Ed25519PrivateKey,
    *,
    public_key: str,
    key_id: str,
    now: datetime,
) -> SignedUserCertificate:
    user_key = _ed25519_public_key(public_key)
    expires_at = now + timedelta(seconds=SSH_CERTIFICATE_TTL_SECONDS)
    builder = (
        SSHCertificateBuilder()
        .public_key(user_key)
        .serial(secrets.randbits(64))
        .type(SSHCertificateType.USER)
        .key_id(key_id.encode("utf-8"))
        .valid_principals([SSH_CERTIFICATE_PRINCIPAL.encode("ascii")])
        .valid_after(int((now - _CERTIFICATE_BACKDATE).timestamp()))
        .valid_before(int(expires_at.timestamp()))
    )
    for extension in _CERTIFICATE_EXTENSIONS:
        builder = builder.add_extension(extension, b"")
    certificate = builder.sign(authority)
    return SignedUserCertificate(
        certificate=certificate.public_bytes().decode("ascii"),
        expires_at=datetime.fromtimestamp(int(expires_at.timestamp()), tz=now.tzinfo),
    )


def _ed25519_public_key(line: str) -> Ed25519PublicKey:
    try:
        key = load_ssh_public_key(line.strip().encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise InvalidInputError("public_key must be an OpenSSH ssh-ed25519 public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise InvalidInputError("public_key must be an OpenSSH ssh-ed25519 public key")
    return key


def _seed(secret: str, label: str, *parts: str) -> bytes:
    if not secret:
        raise ValueError("the workspace credential secret is required to derive SSH identities")
    message = "\x00".join((label, *parts)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


__all__ = [
    "SignedUserCertificate",
    "openssh_private_key",
    "openssh_public_key",
    "pod_host_key",
    "sign_user_certificate",
    "workspace_user_authority",
]

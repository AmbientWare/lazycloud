from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.http.base import HttpModel
from shared.ssh import SSH_CERTIFICATE_PRINCIPAL


class SshCertificateRequest(HttpModel):
    public_key: str = Field(min_length=1, max_length=4096)
    """An OpenSSH ``ssh-ed25519`` public key line to certify."""


class SshCertificateResponse(HttpModel):
    certificate: str
    """The OpenSSH certificate line, written beside the key as ``<key>-cert.pub``."""

    principal: str = SSH_CERTIFICATE_PRINCIPAL
    expires_at: datetime


class SshHostKeyResponse(HttpModel):
    host_public_key: str
    """The pod's ``ssh-ed25519`` host key line, pinned in known_hosts."""


__all__ = ["SshCertificateRequest", "SshCertificateResponse", "SshHostKeyResponse"]

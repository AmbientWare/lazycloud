from __future__ import annotations

from datetime import datetime

from pydantic import Field

from shared.deployments import PodRole
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


class SshHostResponse(HttpModel):
    """A devbox or pod that serves SSH, with what an SSH config needs to reach it."""

    app: str
    pod: str
    role: PodRole
    alias: str
    """The host name SSH config and editors use for it."""

    host_public_key: str
    """The pod's ``ssh-ed25519`` host key line, pinned in known_hosts."""


class SshHostListResponse(HttpModel):
    data: list[SshHostResponse] = Field(default_factory=list)
    next: str = ""
    """Pass as `cursor` for the next page; empty on the last one."""

    workspace: str
    """The workspace's name, which every alias is built from."""


__all__ = [
    "SshCertificateRequest",
    "SshCertificateResponse",
    "SshHostListResponse",
    "SshHostResponse",
]

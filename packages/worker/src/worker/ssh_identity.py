"""Worker payloads for the SSH identity an SSH-enabled container serves.

The control plane derives the identity and hands it only to the worker the
container is assigned to. The worker writes it into the bundle and mounts it
read-only; nothing else keeps a copy.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field
from shared.contracts import ContractModel


class ContainerSshIdentityRequest(ContractModel):
    container_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)


class ContainerSshIdentity(ContractModel):
    host_private_key: str = Field(min_length=1, repr=False)
    """OpenSSH-format ed25519 host private key."""

    user_ca_public_key: str = Field(min_length=1)
    """``ssh-ed25519`` line of the workspace user certificate authority."""


class ContainerSshIdentitySource(Protocol):
    def container_ssh_identity(
        self,
        request: ContainerSshIdentityRequest,
    ) -> ContainerSshIdentity: ...


__all__ = [
    "ContainerSshIdentity",
    "ContainerSshIdentityRequest",
    "ContainerSshIdentitySource",
]

"""SSH access to a running pod.

The container's SSH server is part of the supervisor the worker mounts into it,
so a user image needs no sshd, no host keys, and no port below 1024. It is
reachable only through the authenticated SSH tunnel the API serves, never from
the public internet.

Users authenticate with short-lived OpenSSH certificates signed by the
workspace's user certificate authority. The server trusts that authority rather
than a list of keys, so access ends when a certificate expires and nothing has
to be revoked in the container.
"""

from __future__ import annotations

import re

SSH_WORKER_PORT = 2223
"""Where the supervisor's SSH server listens inside the container.

Next to the dashboard shell's 2222 and away from the ports a workload commonly
binds, so neither can take the other's.
"""

SSH_LOGIN_USER = "root"
"""The account every session logs in as; containers run as root."""

SSH_CERTIFICATE_PRINCIPAL = SSH_LOGIN_USER
SSH_CERTIFICATE_TTL_SECONDS = 12 * 60 * 60
SSH_CONTAINER_IDENTITY_DIR = "/run/lazycloud/ssh"
SSH_CONTAINER_HOST_KEY_PATH = f"{SSH_CONTAINER_IDENTITY_DIR}/ssh_host_ed25519_key"
SSH_CONTAINER_USER_CA_PATH = f"{SSH_CONTAINER_IDENTITY_DIR}/user_ca.pub"

_LABEL_UNSAFE = re.compile(r"[^a-z0-9-]+")


def ssh_host_label(value: str) -> str:
    """A name reduced to what an SSH host alias may hold."""
    label = _LABEL_UNSAFE.sub("-", value.strip().lower()).strip("-")
    if not label:
        raise ValueError(f"cannot build an SSH host name from {value!r}")
    return label


def ssh_host_alias(workspace: str, app: str, pod: str) -> str:
    """The host name a pod answers to in SSH config; pod names repeat across apps.

    The CLI writes it into the SSH config and the dashboard shows it for editors,
    so both have to derive it here.
    """
    return f"lazycloud-{ssh_host_label(workspace)}-{ssh_host_label(app)}-{ssh_host_label(pod)}"


__all__ = [
    "SSH_CERTIFICATE_PRINCIPAL",
    "SSH_CERTIFICATE_TTL_SECONDS",
    "SSH_CONTAINER_HOST_KEY_PATH",
    "SSH_CONTAINER_IDENTITY_DIR",
    "SSH_CONTAINER_USER_CA_PATH",
    "SSH_LOGIN_USER",
    "SSH_WORKER_PORT",
    "ssh_host_alias",
    "ssh_host_label",
]

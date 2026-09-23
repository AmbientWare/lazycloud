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


__all__ = [
    "SSH_CERTIFICATE_PRINCIPAL",
    "SSH_CERTIFICATE_TTL_SECONDS",
    "SSH_CONTAINER_HOST_KEY_PATH",
    "SSH_CONTAINER_IDENTITY_DIR",
    "SSH_CONTAINER_USER_CA_PATH",
    "SSH_LOGIN_USER",
    "SSH_WORKER_PORT",
]

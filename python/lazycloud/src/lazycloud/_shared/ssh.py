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

SSH_LOGIN_USER = "root"
"""The account every session logs in as; containers run as root."""

SSH_HOST_LIST_LIMIT = 100

_LABEL_UNSAFE = re.compile(r"[^a-z0-9-]+")


def ssh_host_label(value: str) -> str:
    """A name reduced to what an SSH host alias may hold."""
    label = _LABEL_UNSAFE.sub("-", value.strip().lower()).strip("-")
    if not label:
        raise ValueError(f"cannot build an SSH host name from {value!r}")
    return label


__all__ = [
    "SSH_LOGIN_USER",
    "ssh_host_label",
]

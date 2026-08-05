from __future__ import annotations

import ipaddress

from starlette.types import Scope


def client_address(scope: Scope, *, header_name: str) -> str:
    """The address rate limiting buckets by, from exactly one configured source.

    With no header configured there is no proxy, so the socket peer is the
    client and every header is attacker-settable. With one configured, only
    that header speaks — the last value, which is the hop the ingress itself
    appended — and the socket peer is the ingress, not the client.

    An absent or unparseable value returns "", and callers treat "" as one
    shared bucket rather than as unlimited.
    """

    if not header_name:
        client = scope.get("client")
        if not client:
            return ""
        return _valid_ip(str(client[0]))
    wanted = header_name.encode("latin-1")
    for name, value in reversed(scope.get("headers") or []):
        if name.lower() == wanted:
            candidate = value.decode("latin-1").split(",")[-1].strip()
            return _valid_ip(candidate)
    return ""


def _valid_ip(candidate: str) -> str:
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""

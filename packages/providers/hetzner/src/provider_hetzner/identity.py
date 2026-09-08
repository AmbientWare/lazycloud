from __future__ import annotations

from hashlib import sha256

from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.provider_identity import (
    ProviderBootstrapNodeEvidence,
    ProviderBootstrapNodeIdentityTarget,
)

from provider_hetzner.client import HetznerClient, HetznerError


def provider_label(provider_ref: str) -> str:
    return sha256(provider_ref.encode()).hexdigest()[:63]


def verify_node(
    client: HetznerClient, *, instance_id: str, target: ProviderBootstrapNodeIdentityTarget
) -> ProviderBootstrapNodeEvidence:
    if not target.launch_id or not instance_id.isdecimal() or int(instance_id) <= 0:
        raise InvalidInputError("invalid Hetzner host identity")
    try:
        server = client.server(int(instance_id))
    except HetznerError:
        raise UpstreamUnavailableError("Hetzner host identity is unavailable") from None
    if server is None:
        raise UpstreamUnavailableError("Hetzner host no longer exists")
    if (
        server.labels.get("lazycloud-managed") != "true"
        or server.labels.get("lazycloud-unit") != target.unit_id
        or server.labels.get("lazycloud-provider") != provider_label(target.provider_ref)
        or server.labels.get("lazycloud-launch") != target.launch_id
        or server.location.name != target.region
    ):
        raise InvalidInputError("Hetzner host does not match the enrolled launch")
    return ProviderBootstrapNodeEvidence(str(server.id), server.location.name)

from __future__ import annotations

from hashlib import sha256

from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.provider_identity import (
    ProviderBootstrapNodeEvidence,
    ProviderBootstrapNodeIdentityTarget,
)

from provider_hyperstack.client import HyperstackClient, HyperstackError


def provider_label(provider_ref: str) -> str:
    return sha256(provider_ref.encode()).hexdigest()[:32]


def labels(values: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, content = value.partition("=")
        if not separator or not key.startswith("lazycloud-"):
            continue
        if key in parsed:
            raise InvalidInputError("Hyperstack instance has duplicate ownership labels")
        parsed[key] = content
    return parsed


def verify_node(
    client: HyperstackClient, *, instance_id: str, target: ProviderBootstrapNodeIdentityTarget
) -> ProviderBootstrapNodeEvidence:
    if not target.launch_id or not instance_id.startswith("lc-"):
        raise InvalidInputError("invalid Hyperstack host identity")
    try:
        server = client.server(instance_id)
    except HyperstackError:
        raise UpstreamUnavailableError("Hyperstack host identity lookup is unavailable") from None
    if server is None:
        raise UpstreamUnavailableError("Hyperstack host no longer exists")
    metadata = labels(server.labels)
    if (
        metadata.get("lazycloud-managed") != "true"
        or metadata.get("lazycloud-unit") != target.unit_id
        or metadata.get("lazycloud-provider") != provider_label(target.provider_ref)
        or metadata.get("lazycloud-launch") != target.launch_id
        or server.environment.region != target.region
        or server.status not in {"ACTIVE", "BUILD"}
    ):
        raise InvalidInputError("Hyperstack host does not match the enrolled launch")
    return ProviderBootstrapNodeEvidence(instance_id=server.name, region=server.environment.region)

from __future__ import annotations

import re
from uuid import UUID

from shared.errors import InvalidInputError, UpstreamUnavailableError
from shared.provider_identity import (
    ProviderBootstrapNodeEvidence,
    ProviderBootstrapNodeIdentityTarget,
)

from provider_ovh.client import OvhClient, OvhError


def verify_node(
    client: OvhClient,
    *,
    instance_id: str,
    target: ProviderBootstrapNodeIdentityTarget,
) -> ProviderBootstrapNodeEvidence:
    try:
        instance_id = str(UUID(instance_id))
        launch_id = str(UUID(target.launch_id))
    except ValueError:
        raise InvalidInputError("invalid OVHcloud host identity") from None
    try:
        server = client.instance(instance_id, target.region)
    except OvhError:
        raise UpstreamUnavailableError("OVHcloud host identity is unavailable") from None
    if server is None:
        raise UpstreamUnavailableError("OVHcloud host no longer exists")
    expected_name = rf"lc-{re.escape(target.unit_id)}-[1-9][0-9]*-[0-9]+-[0-9a-f]{{64}}-{launch_id}"
    if re.fullmatch(expected_name, server.name) is None or server.region != target.region:
        raise InvalidInputError("OVHcloud host does not match the enrolled launch")
    return ProviderBootstrapNodeEvidence(server.id, server.region)

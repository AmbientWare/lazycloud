from __future__ import annotations

import http.client
from dataclasses import dataclass
from hashlib import sha256

from shared.errors import InvalidInputError, UpstreamUnavailableError

from provider_hetzner.client import HetznerClient


@dataclass(frozen=True, slots=True)
class HetznerNodeEvidence:
    instance_id: str
    location: str


@dataclass(frozen=True, slots=True)
class HetznerNodeIdentityTarget:
    provider_ref: str
    unit_id: str
    launch_id: str
    region: str


def provider_label(provider_ref: str) -> str:
    return sha256(provider_ref.encode()).hexdigest()[:63]


def verify_node(
    client: HetznerClient, *, instance_id: str, target: HetznerNodeIdentityTarget
) -> HetznerNodeEvidence:
    if not target.launch_id or not instance_id.isdecimal() or int(instance_id) <= 0:
        raise InvalidInputError("invalid Hetzner host identity")
    server = client.server(int(instance_id))
    if server is None:
        raise UpstreamUnavailableError("Hetzner host no longer exists")
    if (
        server.labels.get("lazycloud-managed") != "true"
        or server.labels.get("lazycloud-unit") != target.unit_id
        or server.labels.get("lazycloud-provider") != provider_label(target.provider_ref)
        or server.labels.get("lazycloud-launch") != target.launch_id
        or server.datacenter.location.name != target.region
    ):
        raise InvalidInputError("Hetzner host does not match the enrolled launch")
    return HetznerNodeEvidence(str(server.id), server.datacenter.location.name)


def node_evidence() -> HetznerNodeEvidence:
    values: list[str] = []
    for field in ("instance-id", "availability-zone"):
        connection = http.client.HTTPConnection("169.254.169.254", timeout=2)
        try:
            connection.request("GET", f"/hetzner/v1/metadata/{field}")
            response = connection.getresponse()
            body = response.read(257)
            if response.status != 200 or len(body) > 256:
                raise UpstreamUnavailableError("Hetzner metadata is unavailable")
            values.append(body.decode("ascii").strip())
        except (OSError, http.client.HTTPException, UnicodeError) as exc:
            raise UpstreamUnavailableError("Hetzner metadata is unavailable") from exc
        finally:
            connection.close()
    instance_id, zone = values
    location = zone.rsplit("-dc", 1)[0]
    if not instance_id.isdecimal() or int(instance_id) <= 0:
        raise InvalidInputError("Hetzner metadata contains an invalid instance ID")
    if location not in {"ash", "hil", "fsn1", "nbg1", "hel1", "sin"}:
        raise InvalidInputError("Hetzner metadata contains an unknown location")
    return HetznerNodeEvidence(instance_id, location)

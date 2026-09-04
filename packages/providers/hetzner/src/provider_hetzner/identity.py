from __future__ import annotations

import http.client
from dataclasses import dataclass

from shared.errors import InvalidInputError, UpstreamUnavailableError


@dataclass(frozen=True, slots=True)
class HetznerNodeEvidence:
    instance_id: str
    location: str


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

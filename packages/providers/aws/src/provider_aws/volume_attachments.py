"""How many EBS volumes this instance has for disks, before any disk is attached.

Each durable disk on an EC2 worker gets its own EBS volume. The machine reports
its instance type's EBS attachment limit from the instance catalog, less what
the instance launched with: the root volume, any other volume in its launch
mapping, and, on a shared limit, each network interface beyond the primary.
Instance metadata describes the launch, not current attachments, so this count
never includes disk volumes. The scheduler subtracts those per machine using
the disks' volume records.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .instance_catalog import AwsInstanceCatalogEntry, aws_instance_catalog_entry
from .instance_metadata import (
    AWS_IMDS_MAX_TEXT_BYTES,
    AWS_IMDS_TIMEOUT_SECONDS,
    AWS_IMDS_TOKEN_TTL_SECONDS,
    AwsDirectInstanceMetadataTransport,
    AwsInstanceMetadataTransport,
)


class AwsVolumeAttachmentError(RuntimeError):
    pass


def disk_volume_slots(
    instance: AwsInstanceCatalogEntry, *, launched_volumes: int, network_interfaces: int
) -> int:
    """Volumes left for disks once the instance's launch-time attachments are counted."""
    extra_interfaces = 0 if instance.ebs_volume_limit_dedicated else network_interfaces - 1
    return max(instance.ebs_volume_limit - launched_volumes - max(extra_interfaces, 0), 0)


@dataclass(frozen=True, slots=True)
class AwsInstanceDiskVolumeSlots:
    """Reads this instance's type and attachments from instance metadata."""

    transport: AwsInstanceMetadataTransport = field(
        default_factory=AwsDirectInstanceMetadataTransport
    )
    timeout_seconds: float = AWS_IMDS_TIMEOUT_SECONDS

    def slots(self) -> int:
        token = self._text(
            method="PUT",
            path="/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": str(AWS_IMDS_TOKEN_TTL_SECONDS)},
        )
        headers = {"X-aws-ec2-metadata-token": token}
        instance_type = self._text(
            method="GET", path="/latest/meta-data/instance-type", headers=headers
        )
        mappings = self._text(
            method="GET", path="/latest/meta-data/block-device-mapping/", headers=headers
        ).split()
        # "ami" and "root" both name the root volume; "ephemeralN" is instance
        # store, which the shared figure already excludes.
        launched = 1 + sum(1 for name in mappings if name.startswith("ebs"))
        interfaces = self._text(
            method="GET", path="/latest/meta-data/network/interfaces/macs/", headers=headers
        ).split()
        try:
            instance = aws_instance_catalog_entry(instance_type)
        except ValueError as exc:
            raise AwsVolumeAttachmentError(
                f"instance type {instance_type!r} has no recorded EBS volume limit: {exc}"
            ) from exc
        return disk_volume_slots(
            instance,
            launched_volumes=launched,
            network_interfaces=len(interfaces),
        )

    def _text(self, *, method: str, path: str, headers: dict[str, str]) -> str:
        response = self.transport.request(
            method=method,
            path=path,
            headers=headers,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=AWS_IMDS_MAX_TEXT_BYTES,
        )
        if response.status_code != 200:
            raise AwsVolumeAttachmentError(
                f"EC2 metadata {path} failed with status {response.status_code}"
            )
        value = response.body.decode("utf-8").strip()
        if not value:
            raise AwsVolumeAttachmentError(f"EC2 metadata {path} returned nothing")
        return value


__all__ = [
    "AwsInstanceDiskVolumeSlots",
    "AwsVolumeAttachmentError",
    "disk_volume_slots",
]

"""EBS volumes that back durable disks, one volume per disk.

Every volume carries the managed tag the connection policy keys on, a resource
tag marking it a disk volume, and tags naming its deployment, workspace and
disk. The policy lets the control plane create a volume only with those tags,
and attach, detach or delete only a volume that carries them. A volume someone
else made in the same account is out of reach by permission as well as by the
listing filter.

A disk volume attaches at a device name no launch template uses. That is how a
machine's storage evidence tells its root volume from an attached disk volume;
see `is_disk_volume_device`.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypedDict, TypeGuard

from botocore.exceptions import BotoCoreError, ClientError, WaiterError
from compute.block_volumes import (
    BlockVolume,
    BlockVolumeMissingError,
    BlockVolumeOwner,
    BlockVolumePendingError,
    BlockVolumeProvider,
    BlockVolumeRequest,
    BlockVolumeState,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.errors import ConflictError

from .boto3_clients import has_operations
from .connection_policy import (
    DISK_VOLUME_TAG_KEY,
    DISK_VOLUME_TAG_VALUE,
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
)
from .provider_control import (
    AwsProviderControlError,
    AwsProviderControlErrorCode,
    invalid_response_error,
    upstream_error,
)

AWS_DISK_VOLUME_RESOURCE = DISK_VOLUME_TAG_VALUE
RESOURCE_TAG_KEY = DISK_VOLUME_TAG_KEY
DEPLOYMENT_TAG_KEY = "cloud-pool:deployment"
WORKSPACE_TAG_KEY = "cloud-pool:workspace"
DISK_TAG_KEY = "cloud-pool:disk"
VOLUME_TOKEN_TAG_KEY = "cloud-pool:volume-token"

# Launch templates map only the root device, which is /dev/xvda or the AMI's own
# /dev/sda1, so a disk volume taking a name from this range never collides with
# it. The worker finds the device by the volume id, not by this name.
_DISK_DEVICE_NAMES = tuple(f"/dev/sd{letter}" for letter in "fghijklmnopqrstuvwxyz")

_GIB = 1024**3
_WAIT_DELAY_SECONDS = 2


def is_disk_volume_device(device_name: str) -> bool:
    """Whether a block device mapping is a disk volume rather than the machine's own storage."""
    return device_name in _DISK_DEVICE_NAMES


class _Filter(TypedDict):
    Name: str
    Values: list[str]


class _Tag(TypedDict):
    Key: str
    Value: str


class _TagSpecification(TypedDict):
    ResourceType: str
    Tags: list[_Tag]


class _EbsModification(TypedDict):
    DeleteOnTermination: bool
    VolumeId: str


class _BlockDeviceModification(TypedDict):
    DeviceName: str
    Ebs: _EbsModification


class _WaiterConfig(TypedDict):
    Delay: int
    MaxAttempts: int


class _VolumeWaiter(Protocol):
    def wait(self, *, VolumeIds: list[str], WaiterConfig: _WaiterConfig) -> None: ...


class AwsBlockVolumeEc2Client(Protocol):
    def create_volume(
        self,
        *,
        AvailabilityZone: str,
        Size: int,
        VolumeType: str,
        Throughput: int,
        Encrypted: bool,
        ClientToken: str,
        TagSpecifications: list[_TagSpecification],
    ) -> Mapping[str, object]: ...
    def attach_volume(
        self, *, Device: str, InstanceId: str, VolumeId: str
    ) -> Mapping[str, object]: ...
    def detach_volume(self, *, InstanceId: str, VolumeId: str) -> Mapping[str, object]: ...
    def delete_volume(self, *, VolumeId: str) -> Mapping[str, object]: ...
    def describe_volumes(
        self,
        *,
        VolumeIds: list[str] | None = None,
        Filters: list[_Filter] | None = None,
        NextToken: str = "",
    ) -> Mapping[str, object]: ...
    def describe_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]: ...
    def describe_availability_zones(self, *, ZoneIds: list[str]) -> Mapping[str, object]: ...
    def modify_instance_attribute(
        self, *, InstanceId: str, BlockDeviceMappings: list[_BlockDeviceModification]
    ) -> Mapping[str, object]: ...
    def get_waiter(self, waiter_name: str) -> _VolumeWaiter: ...


def is_block_volume_client(value: object) -> TypeGuard[AwsBlockVolumeEc2Client]:
    return has_operations(
        value,
        (
            "attach_volume",
            "create_volume",
            "delete_volume",
            "describe_availability_zones",
            "describe_instances",
            "describe_volumes",
            "detach_volume",
            "get_waiter",
            "modify_instance_attribute",
        ),
    )


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Attachment(_Response):
    instance_id: str = Field(default="", alias="InstanceId")
    state: str = Field(default="", alias="State")
    device: str = Field(default="", alias="Device")
    delete_on_termination: bool = Field(default=False, alias="DeleteOnTermination")


class _VolumeTag(_Response):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class _Volume(_Response):
    volume_id: str = Field(alias="VolumeId")
    state: str = Field(alias="State")
    size_gib: int = Field(default=0, alias="Size")
    zone_id: str = Field(default="", alias="AvailabilityZoneId")
    created_at: datetime | None = Field(default=None, alias="CreateTime")
    attachments: tuple[_Attachment, ...] = Field(default=(), alias="Attachments")
    tags: tuple[_VolumeTag, ...] = Field(default=(), alias="Tags")


class _Volumes(_Response):
    volumes: tuple[_Volume, ...] = Field(default=(), alias="Volumes")
    next_token: str = Field(default="", alias="NextToken")


class _Zone(_Response):
    name: str = Field(alias="ZoneName")
    zone_id: str = Field(alias="ZoneId")


class _Zones(_Response):
    zones: tuple[_Zone, ...] = Field(default=(), alias="AvailabilityZones")


class _InstanceDevice(_Response):
    name: str = Field(default="", alias="DeviceName")


class _Instance(_Response):
    devices: tuple[_InstanceDevice, ...] = Field(default=(), alias="BlockDeviceMappings")


class _Reservation(_Response):
    instances: tuple[_Instance, ...] = Field(default=(), alias="Instances")


class _Instances(_Response):
    reservations: tuple[_Reservation, ...] = Field(default=(), alias="Reservations")


_LIVE_ATTACHMENT_STATES = frozenset({"attaching", "attached", "busy"})


@dataclass(frozen=True, slots=True)
class AwsBlockVolumes(BlockVolumeProvider):
    ec2: AwsBlockVolumeEc2Client
    sleep: Callable[[float], None] = field(default=time.sleep)
    monotonic: Callable[[], float] = field(default=time.monotonic)

    def create_volume(self, request: BlockVolumeRequest, *, wait_seconds: float) -> BlockVolume:
        deadline = self.monotonic() + wait_seconds
        existing = self._by_token(request.token)
        if existing is None:
            response = self._call(
                "create disk volume",
                lambda: self.ec2.create_volume(
                    AvailabilityZone=self._zone_name(request.zone),
                    Size=max(1, math.ceil(request.size_bytes / _GIB)),
                    VolumeType="gp3",
                    Throughput=request.throughput_mibps,
                    Encrypted=True,
                    ClientToken=request.token,
                    TagSpecifications=[{"ResourceType": "volume", "Tags": _tags(request)}],
                ),
            )
            volume_id = _parse(_Volume, response, operation="create disk volume").volume_id
        else:
            if existing.state in {"deleting", "deleted", "error"}:
                raise AwsProviderControlError(
                    AwsProviderControlErrorCode.InvalidResponse,
                    operation="create disk volume",
                    detail=f"volume {existing.volume_id} from this creation is {existing.state}",
                )
            volume_id = existing.volume_id
        self._wait(
            "volume_available",
            volume_id,
            operation="wait for disk volume creation",
            deadline=deadline,
        )
        described = self._describe(volume_id)
        if described is None:
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.ResourceNotFound,
                operation="create disk volume",
                detail=f"volume {volume_id} disappeared after creation",
            )
        return _block_volume(described)

    def attach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        deadline = self.monotonic() + wait_seconds
        volume = self._require(volume_id)
        attachment = _live_attachment(volume)
        if attachment is not None and attachment.instance_id != instance_id:
            raise ConflictError(
                f"volume {volume_id} is attached to {attachment.instance_id}, not {instance_id}"
            )
        if attachment is None:
            if volume.state == "creating" or any(
                item.state == "detaching" for item in volume.attachments
            ):
                self._wait(
                    "volume_available",
                    volume_id,
                    operation="wait for disk volume",
                    deadline=deadline,
                )
            device = self._attach(volume_id, instance_id=instance_id)
        else:
            device = attachment.device
        attached = self._await_attached(volume_id, instance_id=instance_id, deadline=deadline)
        if not attached.delete_on_termination:
            # A terminated instance deletes its disk volumes. The object store
            # holds the durable copy, and nothing else would delete them.
            self._call(
                "delete disk volume with its instance",
                lambda: self.ec2.modify_instance_attribute(
                    InstanceId=instance_id,
                    BlockDeviceMappings=[
                        {
                            "DeviceName": attached.device or device,
                            "Ebs": {"DeleteOnTermination": True, "VolumeId": volume_id},
                        }
                    ],
                ),
            )

    def detach_volume(self, volume_id: str, *, instance_id: str, wait_seconds: float) -> None:
        deadline = self.monotonic() + wait_seconds
        volume = self._describe(volume_id)
        if volume is None or volume.state in {"deleting", "deleted"}:
            return
        attachment = _live_attachment(volume)
        if attachment is not None:
            if attachment.instance_id != instance_id:
                raise ConflictError(
                    f"volume {volume_id} is attached to {attachment.instance_id}, not {instance_id}"
                )
            try:
                self.ec2.detach_volume(InstanceId=instance_id, VolumeId=volume_id)
            except ClientError as exc:
                code = _error_code(exc)
                if code == "InvalidVolume.NotFound":
                    return
                if code != "IncorrectState":
                    raise _control_error(exc, operation="detach disk volume") from exc
            except BotoCoreError as exc:
                raise upstream_error(exc, operation="detach disk volume") from exc
        elif volume.state == "available":
            return
        self._wait(
            "volume_available",
            volume_id,
            operation="wait for disk volume detach",
            deadline=deadline,
        )

    def delete_volume(self, volume_id: str) -> None:
        try:
            self.ec2.delete_volume(VolumeId=volume_id)
        except ClientError as exc:
            if _error_code(exc) == "InvalidVolume.NotFound":
                return
            raise _control_error(exc, operation="delete disk volume") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="delete disk volume") from exc

    def describe_volumes(self, *, deployment: str) -> tuple[BlockVolume, ...]:
        filters: list[_Filter] = [
            {"Name": f"tag:{MANAGED_TAG_KEY}", "Values": [MANAGED_TAG_VALUE]},
            {"Name": f"tag:{RESOURCE_TAG_KEY}", "Values": [AWS_DISK_VOLUME_RESOURCE]},
            {"Name": f"tag:{DEPLOYMENT_TAG_KEY}", "Values": [deployment]},
        ]
        volumes: list[BlockVolume] = []
        token = ""
        while True:
            page = _parse(
                _Volumes,
                self._call(
                    "list disk volumes",
                    lambda page_token=token: self.ec2.describe_volumes(
                        Filters=filters, NextToken=page_token
                    ),
                ),
                operation="list disk volumes",
            )
            volumes.extend(_block_volume(volume) for volume in page.volumes)
            if not page.next_token:
                return tuple(volumes)
            token = page.next_token

    def _attach(self, volume_id: str, *, instance_id: str) -> str:
        used = self._devices(instance_id)
        for device in _DISK_DEVICE_NAMES:
            if device in used:
                continue
            try:
                self.ec2.attach_volume(Device=device, InstanceId=instance_id, VolumeId=volume_id)
            except ClientError as exc:
                code = _error_code(exc)
                if code == "InvalidParameterValue" and "already in use" in _error_message(exc):
                    continue
                if code == "InvalidVolume.NotFound":
                    raise BlockVolumeMissingError(volume_id) from exc
                raise _control_error(exc, operation="attach disk volume") from exc
            except BotoCoreError as exc:
                raise upstream_error(exc, operation="attach disk volume") from exc
            return device
        raise AwsProviderControlError(
            AwsProviderControlErrorCode.InvalidResponse,
            operation="attach disk volume",
            detail=f"instance {instance_id} has no free disk device name",
        )

    def _await_attached(self, volume_id: str, *, instance_id: str, deadline: float) -> _Attachment:
        while True:
            volume = self._require(volume_id)
            for attachment in volume.attachments:
                if attachment.instance_id != instance_id:
                    continue
                if attachment.state == "attached":
                    return attachment
                if attachment.state not in {"attaching", "busy"}:
                    raise AwsProviderControlError(
                        AwsProviderControlErrorCode.InvalidResponse,
                        operation="attach disk volume",
                        detail=f"volume {volume_id} attachment is {attachment.state}",
                    )
            if self.monotonic() >= deadline:
                raise BlockVolumePendingError(f"volume {volume_id} is still attaching")
            self.sleep(_WAIT_DELAY_SECONDS)

    def _devices(self, instance_id: str) -> frozenset[str]:
        described = _parse(
            _Instances,
            self._call(
                "describe disk volume instance",
                lambda: self.ec2.describe_instances(InstanceIds=[instance_id]),
            ),
            operation="describe disk volume instance",
        )
        return frozenset(
            device.name
            for reservation in described.reservations
            for instance in reservation.instances
            for device in instance.devices
        )

    def _zone_name(self, zone_id: str) -> str:
        zones = _parse(
            _Zones,
            self._call(
                "resolve disk volume zone",
                lambda: self.ec2.describe_availability_zones(ZoneIds=[zone_id]),
            ),
            operation="resolve disk volume zone",
        ).zones
        for zone in zones:
            if zone.zone_id == zone_id:
                return zone.name
        raise AwsProviderControlError(
            AwsProviderControlErrorCode.ResourceNotFound,
            operation="resolve disk volume zone",
            detail=f"availability zone {zone_id} is not in this region",
        )

    def _by_token(self, token: str) -> _Volume | None:
        filters: list[_Filter] = [
            {"Name": f"tag:{MANAGED_TAG_KEY}", "Values": [MANAGED_TAG_VALUE]},
            {"Name": f"tag:{VOLUME_TOKEN_TAG_KEY}", "Values": [token]},
        ]
        found = _parse(
            _Volumes,
            self._call(
                "find disk volume creation",
                lambda: self.ec2.describe_volumes(Filters=filters),
            ),
            operation="find disk volume creation",
        ).volumes
        return found[0] if found else None

    def _require(self, volume_id: str) -> _Volume:
        volume = self._describe(volume_id)
        if volume is None or volume.state in {"deleting", "deleted"}:
            raise BlockVolumeMissingError(volume_id)
        return volume

    def _describe(self, volume_id: str) -> _Volume | None:
        try:
            response = self.ec2.describe_volumes(VolumeIds=[volume_id])
        except ClientError as exc:
            if _error_code(exc) == "InvalidVolume.NotFound":
                return None
            raise _control_error(exc, operation="describe disk volume") from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation="describe disk volume") from exc
        volumes = _parse(_Volumes, response, operation="describe disk volume").volumes
        return volumes[0] if volumes else None

    def _wait(self, waiter: str, volume_id: str, *, operation: str, deadline: float) -> None:
        remaining = max(deadline - self.monotonic(), 0.0)
        try:
            self.ec2.get_waiter(waiter).wait(
                VolumeIds=[volume_id],
                WaiterConfig={
                    "Delay": _WAIT_DELAY_SECONDS,
                    "MaxAttempts": max(1, math.ceil(remaining / _WAIT_DELAY_SECONDS)),
                },
            )
        except WaiterError as exc:
            if "Max attempts exceeded" in str(exc):
                raise BlockVolumePendingError(f"volume {volume_id}: {operation}") from exc
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.UpstreamUnavailable,
                operation=operation,
                detail=f"volume {volume_id}: {exc}",
            ) from exc
        except ClientError as exc:
            raise _control_error(exc, operation=operation) from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation=operation) from exc

    @staticmethod
    def _call(operation: str, call: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
        try:
            return call()
        except ClientError as exc:
            raise _control_error(exc, operation=operation) from exc
        except BotoCoreError as exc:
            raise upstream_error(exc, operation=operation) from exc


def _tags(request: BlockVolumeRequest) -> list[_Tag]:
    return [
        {"Key": "Name", "Value": f"disk-{request.owner.disk_id}"},
        {"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE},
        {"Key": RESOURCE_TAG_KEY, "Value": AWS_DISK_VOLUME_RESOURCE},
        {"Key": DEPLOYMENT_TAG_KEY, "Value": request.owner.deployment},
        {"Key": WORKSPACE_TAG_KEY, "Value": request.owner.workspace_id},
        {"Key": DISK_TAG_KEY, "Value": request.owner.disk_id},
        {"Key": VOLUME_TOKEN_TAG_KEY, "Value": request.token},
    ]


def _live_attachment(volume: _Volume) -> _Attachment | None:
    return next(
        (item for item in volume.attachments if item.state in _LIVE_ATTACHMENT_STATES), None
    )


_STATES = {
    "creating": BlockVolumeState.Creating,
    "available": BlockVolumeState.Available,
    "in-use": BlockVolumeState.InUse,
    "deleting": BlockVolumeState.Deleting,
    "deleted": BlockVolumeState.Deleted,
}


def _block_volume(volume: _Volume) -> BlockVolume:
    tags = {tag.key: tag.value for tag in volume.tags}
    owner: BlockVolumeOwner | None = None
    if (
        tags.get(MANAGED_TAG_KEY) == MANAGED_TAG_VALUE
        and tags.get(RESOURCE_TAG_KEY) == AWS_DISK_VOLUME_RESOURCE
        and tags.get(DEPLOYMENT_TAG_KEY)
        and tags.get(WORKSPACE_TAG_KEY)
        and tags.get(DISK_TAG_KEY)
    ):
        owner = BlockVolumeOwner(
            deployment=tags[DEPLOYMENT_TAG_KEY],
            workspace_id=tags[WORKSPACE_TAG_KEY],
            disk_id=tags[DISK_TAG_KEY],
        )
    attachment = _live_attachment(volume)
    return BlockVolume(
        volume_id=volume.volume_id,
        zone=volume.zone_id,
        size_bytes=volume.size_gib * _GIB,
        state=_STATES.get(volume.state, BlockVolumeState.Error),
        attached_instance_id=attachment.instance_id if attachment is not None else "",
        owner=owner,
        creation_token=tags.get(VOLUME_TOKEN_TAG_KEY, ""),
        created_at=volume.created_at,
    )


def _parse[ResponseT: BaseModel](
    model: type[ResponseT], response: Mapping[str, object], *, operation: str
) -> ResponseT:
    try:
        return model.model_validate(response)
    except ValidationError as exc:
        raise invalid_response_error(operation, "AWS returned an invalid response") from exc


def _error_code(exc: ClientError) -> str:
    raw = exc.response.get("Error", {})
    return str(raw.get("Code", "")) if isinstance(raw, Mapping) else ""


def _error_message(exc: ClientError) -> str:
    raw = exc.response.get("Error", {})
    return str(raw.get("Message", "")) if isinstance(raw, Mapping) else ""


def _control_error(exc: ClientError, *, operation: str) -> AwsProviderControlError:
    code = _error_code(exc)
    normalized = code.casefold()
    if normalized in {"accessdenied", "accessdeniedexception", "unauthorizedoperation"}:
        category = AwsProviderControlErrorCode.PermissionDenied
    elif "notfound" in normalized:
        category = AwsProviderControlErrorCode.ResourceNotFound
    else:
        category = AwsProviderControlErrorCode.UpstreamUnavailable
    return AwsProviderControlError(
        category,
        operation=operation,
        detail=_error_message(exc).strip() or code.strip() or "AWS request failed",
    )


__all__ = [
    "AWS_DISK_VOLUME_RESOURCE",
    "AwsBlockVolumeEc2Client",
    "AwsBlockVolumes",
    "is_block_volume_client",
    "is_disk_volume_device",
]

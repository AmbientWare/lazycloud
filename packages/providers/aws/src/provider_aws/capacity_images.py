"""Platform-account sharing of pre-baked capacity images with connected accounts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self, TypeGuard, cast

from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError

from .account_connection import connection_profile_name
from .boto3_clients import is_boto3_client_factory
from .provider_control import (
    AwsProviderControlError,
    AwsProviderControlErrorCode,
    upstream_error,
)

_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{12}$")
_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")


class AwsCapacityImageEc2Client(Protocol):
    def describe_images(self, *, ImageIds: Sequence[str]) -> Mapping[str, object]: ...

    def modify_image_attribute(
        self,
        *,
        ImageId: str,
        LaunchPermission: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class AwsCapacityImageClientFactory(Protocol):
    def __call__(self, *, region_name: str) -> AwsCapacityImageEc2Client: ...


@dataclass(frozen=True, slots=True)
class Boto3AwsCapacityImageSharing:
    """Grants EC2 launch permission on platform-owned capacity AMIs.

    Capacity AMIs are baked in the platform account by the node-image workflow, so
    this control always runs with the platform default credential chain, never
    an assumed customer connection role.
    """

    client_factory: AwsCapacityImageClientFactory

    @classmethod
    def from_default_chain(cls) -> Self:
        return cls(_default_ec2_client)

    def grant_launch_permission(
        self,
        *,
        region: str,
        account_id: str,
        ami_ids: Sequence[str],
    ) -> tuple[str, ...]:
        normalized_region = region.strip().lower()
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError("invalid AWS capacity image region")
        if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ValueError("AWS capacity image account ID must contain exactly 12 digits")
        normalized = tuple(sorted({ami_id.strip().lower() for ami_id in ami_ids}))
        if any(not _AMI_PATTERN.fullmatch(ami_id) for ami_id in normalized):
            raise ValueError("invalid AWS capacity image ID")
        if not normalized:
            return ()
        client = self.client_factory(region_name=normalized_region)
        visibility = _image_visibility(client, normalized)
        for ami_id in normalized:
            image = visibility.get(ami_id)
            if image is None or image.public or image.owner_id == account_id:
                # Public catalog images are already launchable by every account,
                # and an account that owns an image cannot grant itself launch
                # permission. Only privately owned platform images need sharing.
                continue
            try:
                client.modify_image_attribute(
                    ImageId=ami_id,
                    LaunchPermission={"Add": [{"UserId": account_id}]},
                )
            except ClientError as exc:
                raise _client_error(exc, operation="share capacity image") from exc
            except BotoCoreError as exc:
                raise upstream_error(exc, operation="share capacity image") from exc
        return normalized


@dataclass(frozen=True, slots=True)
class _ImageVisibility:
    owner_id: str
    public: bool


def _image_visibility(
    client: AwsCapacityImageEc2Client,
    ami_ids: Sequence[str],
) -> dict[str, _ImageVisibility]:
    try:
        described = client.describe_images(ImageIds=list(ami_ids))
    except ClientError as exc:
        raise _client_error(exc, operation="describe capacity images") from exc
    except BotoCoreError as exc:
        raise upstream_error(exc, operation="describe capacity images") from exc
    images: object = described.get("Images")
    if not isinstance(images, list):
        return {}
    visibility: dict[str, _ImageVisibility] = {}
    for entry in cast("list[object]", images):
        if not isinstance(entry, Mapping):
            continue
        image = cast("Mapping[str, object]", entry)
        image_id = image.get("ImageId")
        owner_id = image.get("OwnerId")
        if isinstance(image_id, str) and isinstance(owner_id, str):
            visibility[image_id] = _ImageVisibility(
                owner_id=owner_id,
                public=image.get("Public") is True,
            )
    return visibility


def _is_capacity_image_client(value: object) -> TypeGuard[AwsCapacityImageEc2Client]:
    return callable(getattr(value, "modify_image_attribute", None)) and callable(
        getattr(value, "describe_images", None)
    )


def _default_ec2_client(*, region_name: str) -> AwsCapacityImageEc2Client:
    # The control principal, like every other capacity call: the EC2 image
    # grants live on that role and not on the one the workload runs as.
    profile = connection_profile_name()
    source: object = Session(region_name=region_name, profile_name=profile or None)
    if not is_boto3_client_factory(source):
        raise RuntimeError("boto3 session lacks the client factory operation")
    candidate = source.client("ec2")
    if not _is_capacity_image_client(candidate):
        raise RuntimeError("boto3 EC2 client lacks the image attribute operation")
    return candidate


def _client_error(exc: ClientError, *, operation: str) -> AwsProviderControlError:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    message = str(error.get("Message", "")) if isinstance(error, Mapping) else ""
    normalized = code.casefold()
    if normalized in {"accessdenied", "accessdeniedexception", "unauthorizedoperation"}:
        error_code = AwsProviderControlErrorCode.PermissionDenied
    elif "notfound" in normalized:
        error_code = AwsProviderControlErrorCode.ResourceNotFound
    else:
        error_code = AwsProviderControlErrorCode.UpstreamUnavailable
    return AwsProviderControlError(
        error_code,
        operation=operation,
        detail=message.strip() or code.strip() or "AWS request failed",
    )


__all__ = [
    "AwsCapacityImageClientFactory",
    "AwsCapacityImageEc2Client",
    "Boto3AwsCapacityImageSharing",
]

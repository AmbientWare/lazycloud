from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, TypedDict, TypeGuard, overload
from urllib.parse import urlparse

from boto3.session import Session
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from shared.app_identity import AGENT_NAME, STATE_DIR
from shared.tailscale_install import TAILSCALE_AMD64_SHA256, TAILSCALE_INSTALL_VERSION

from .account_connection import AwsAccountConnectionTarget
from .instance_catalog import aws_instance_catalog_entry, aws_managed_capacity_resource_name
from .provider_control import (
    AwsProviderControlError,
    AwsProviderControlErrorCode,
)

AWS_MANAGED_POOL_TAG = "cloud-pool:managed-by"
AWS_MANAGED_POOL_TAG_VALUE = "control-plane"

type _AwsResponseValue = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | datetime
    | list[_AwsResponseValue]
    | dict[str, _AwsResponseValue]
)

_AWS_RESPONSE = TypeAdapter(dict[str, _AwsResponseValue])

_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENROLLMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_INSTANCE_ID_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_WORKER_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")


class AwsManagedPoolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AwsManagedPoolBootstrap(AwsManagedPoolModel):
    control_plane_url: str
    enrollment_request_id: str = Field(pattern=_ENROLLMENT_PATTERN.pattern)
    agent_version: str = Field(pattern=_SAFE_VERSION_PATTERN.pattern)
    agent_sha256: str = Field(pattern=_DIGEST_PATTERN.pattern)
    agent_artifact_url: str
    worker_image_digest: str = Field(pattern=_WORKER_IMAGE_PATTERN.pattern)
    gpu_count: int = Field(default=0, ge=0, le=8)

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("control-plane URL must be an HTTPS origin")
        return url

    @field_validator("agent_artifact_url")
    @classmethod
    def validate_agent_artifact_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("agent artifact URL must be an HTTPS URL without credentials")
        return url


class AwsManagedPoolArtifacts(AwsManagedPoolModel):
    agent_version: str = Field(pattern=_SAFE_VERSION_PATTERN.pattern)
    agent_sha256: str = Field(pattern=_DIGEST_PATTERN.pattern)
    worker_image_digest: str = Field(pattern=_WORKER_IMAGE_PATTERN.pattern)
    cpu_ami_id: str | None = Field(default=None, pattern=_AMI_PATTERN.pattern)
    gpu_ami_id: str | None = Field(default=None, pattern=_AMI_PATTERN.pattern)


class AwsManagedPoolSpec(AwsManagedPoolModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    pool_name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    region: str = Field(pattern=_REGION_PATTERN.pattern)
    instance_type: str
    ami_id: str = Field(pattern=_AMI_PATTERN.pattern)
    desired_nodes: int = Field(ge=0, le=100)
    max_nodes: int = Field(ge=1, le=100)
    root_volume_gib: int = Field(ge=50, le=2048)
    node_instance_profile_arn: str
    vpc_id: str = Field(min_length=1)
    subnet_ids: tuple[str, str]
    security_group_id: str = Field(min_length=1)
    bootstrap: AwsManagedPoolBootstrap

    @field_validator("instance_type")
    @classmethod
    def validate_instance_type(cls, value: str) -> str:
        instance_type = value.strip().lower()
        aws_instance_catalog_entry(instance_type)
        return instance_type

    @model_validator(mode="after")
    def validate_capacity_and_profile(self) -> AwsManagedPoolSpec:
        if self.desired_nodes > self.max_nodes:
            raise ValueError("desired_nodes cannot exceed max_nodes")
        if ":instance-profile/" not in self.node_instance_profile_arn:
            raise ValueError("node instance profile ARN is invalid")
        return self

    @property
    def resource_key(self) -> str:
        return hashlib.sha256(f"{self.workspace_id}\0{self.pool_name}".encode()).hexdigest()[:24]

    @property
    def autoscaling_group_name(self) -> str:
        return aws_managed_capacity_resource_name(
            "asg", self.workspace_id, self.pool_name, max_length=255
        )

    @property
    def launch_template_name(self) -> str:
        return aws_managed_capacity_resource_name(
            "nodes", self.workspace_id, self.pool_name, max_length=128
        )


class AwsManagedPoolPhase(StrEnum):
    Provisioning = "provisioning"
    Ready = "ready"
    Deleting = "deleting"
    Deleted = "deleted"


class AwsManagedPoolResourceIds(AwsManagedPoolModel):
    launch_template_id: str | None = None
    launch_template_version: int | None = None
    autoscaling_group_name: str | None = None

    @property
    def complete(self) -> bool:
        return (
            self.launch_template_id is not None
            and self.launch_template_version is not None
            and self.autoscaling_group_name is not None
        )


class AwsManagedPoolInstance(AwsManagedPoolModel):
    instance_id: str = Field(pattern=_INSTANCE_ID_PATTERN.pattern)
    lifecycle_state: str
    health_status: str
    availability_zone: str


class AwsManagedPoolSnapshot(AwsManagedPoolModel):
    phase: AwsManagedPoolPhase
    resource_ids: AwsManagedPoolResourceIds
    desired_nodes: int = Field(ge=0)
    max_nodes: int = Field(ge=0)
    instances: tuple[AwsManagedPoolInstance, ...] = ()


class AwsManagedPoolProvisioningError(AwsProviderControlError):
    def __init__(
        self,
        cause: AwsProviderControlError,
        *,
        resource_ids: AwsManagedPoolResourceIds,
    ) -> None:
        self.resource_ids = resource_ids
        super().__init__(cause.code, operation=cause.operation, detail=cause.detail)


AwsManagedPoolProgressSink = Callable[[AwsManagedPoolResourceIds], None]


class _Tag(TypedDict):
    Key: str
    Value: str


class _Filter(TypedDict):
    Name: str
    Values: list[str]


class _LaunchTemplateRef(TypedDict):
    LaunchTemplateId: str
    Version: str


class _InstanceProfileRef(TypedDict):
    Arn: str


class _EbsSpec(TypedDict):
    VolumeSize: int
    VolumeType: str
    Encrypted: bool
    DeleteOnTermination: bool


class _BlockDeviceMapping(TypedDict):
    DeviceName: str
    Ebs: _EbsSpec


class _MetadataOptions(TypedDict):
    HttpEndpoint: str
    HttpTokens: str
    HttpPutResponseHopLimit: int
    InstanceMetadataTags: str


class _TagSpecification(TypedDict):
    ResourceType: str
    Tags: list[_Tag]


class _LaunchTemplateData(TypedDict):
    ImageId: str
    InstanceType: str
    BlockDeviceMappings: list[_BlockDeviceMapping]
    IamInstanceProfile: _InstanceProfileRef
    SecurityGroupIds: list[str]
    MetadataOptions: _MetadataOptions
    TagSpecifications: list[_TagSpecification]
    UserData: str


class AwsManagedPoolEc2Client(Protocol):
    def describe_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]: ...
    def describe_volumes(
        self,
        *,
        VolumeIds: list[str] | None = None,
        Filters: list[_Filter] | None = None,
    ) -> Mapping[str, object]: ...
    def describe_launch_templates(
        self, *, LaunchTemplateNames: list[str]
    ) -> Mapping[str, object]: ...
    def describe_launch_template_versions(
        self, *, LaunchTemplateId: str, Versions: list[str]
    ) -> Mapping[str, object]: ...
    def create_launch_template(
        self,
        *,
        LaunchTemplateName: str,
        VersionDescription: str,
        LaunchTemplateData: _LaunchTemplateData,
        TagSpecifications: list[_TagSpecification],
    ) -> Mapping[str, object]: ...
    def create_launch_template_version(
        self,
        *,
        LaunchTemplateId: str,
        VersionDescription: str,
        LaunchTemplateData: _LaunchTemplateData,
    ) -> Mapping[str, object]: ...
    def modify_launch_template(
        self, *, LaunchTemplateId: str, DefaultVersion: str
    ) -> Mapping[str, object]: ...
    def delete_launch_template(self, *, LaunchTemplateId: str) -> Mapping[str, object]: ...


class AwsManagedPoolAutoScalingClient(Protocol):
    def describe_auto_scaling_groups(
        self, *, AutoScalingGroupNames: list[str]
    ) -> Mapping[str, object]: ...
    def create_auto_scaling_group(
        self,
        *,
        AutoScalingGroupName: str,
        MinSize: int,
        MaxSize: int,
        DesiredCapacity: int,
        HealthCheckType: str,
        HealthCheckGracePeriod: int,
        VPCZoneIdentifier: str,
        LaunchTemplate: _LaunchTemplateRef,
        Tags: list[Mapping[str, object]],
    ) -> Mapping[str, object]: ...
    def update_auto_scaling_group(
        self,
        *,
        AutoScalingGroupName: str,
        MinSize: int,
        MaxSize: int,
        DesiredCapacity: int,
        VPCZoneIdentifier: str,
        LaunchTemplate: _LaunchTemplateRef,
    ) -> Mapping[str, object]: ...
    def set_desired_capacity(
        self, *, AutoScalingGroupName: str, DesiredCapacity: int, HonorCooldown: bool
    ) -> Mapping[str, object]: ...
    def terminate_instance_in_auto_scaling_group(
        self, *, InstanceId: str, ShouldDecrementDesiredCapacity: bool
    ) -> Mapping[str, object]: ...
    def delete_auto_scaling_group(
        self, *, AutoScalingGroupName: str, ForceDelete: bool
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class AwsManagedPoolClients:
    ec2: AwsManagedPoolEc2Client
    autoscaling: AwsManagedPoolAutoScalingClient


class AwsManagedPoolClientProvider(Protocol):
    def assume(self, target: AwsAccountConnectionTarget) -> AwsManagedPoolClients: ...


class AwsManagedPoolStsClient(Protocol):
    def assume_role(
        self, *, RoleArn: str, RoleSessionName: str, ExternalId: str, DurationSeconds: int
    ) -> Mapping[str, object]: ...


class AwsManagedPoolSession(Protocol):
    @overload
    def client(self, service_name: Literal["sts"]) -> AwsManagedPoolStsClient: ...
    @overload
    def client(self, service_name: Literal["ec2"]) -> AwsManagedPoolEc2Client: ...
    @overload
    def client(self, service_name: Literal["autoscaling"]) -> AwsManagedPoolAutoScalingClient: ...


class AwsManagedPoolSessionFactory(Protocol):
    def __call__(
        self,
        *,
        region_name: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
    ) -> AwsManagedPoolSession: ...


class _Boto3ClientFactory(Protocol):
    def client(self, service_name: str) -> object: ...


def _is_boto3_client_factory(value: object) -> TypeGuard[_Boto3ClientFactory]:
    return callable(getattr(value, "client", None))


@dataclass(frozen=True, slots=True)
class _Boto3ManagedPoolSession:
    session: Session

    @overload
    def client(self, service_name: Literal["sts"]) -> AwsManagedPoolStsClient: ...

    @overload
    def client(self, service_name: Literal["ec2"]) -> AwsManagedPoolEc2Client: ...

    @overload
    def client(self, service_name: Literal["autoscaling"]) -> AwsManagedPoolAutoScalingClient: ...

    def client(
        self, service_name: Literal["sts", "ec2", "autoscaling"]
    ) -> AwsManagedPoolStsClient | AwsManagedPoolEc2Client | AwsManagedPoolAutoScalingClient:
        source: object = self.session
        if not _is_boto3_client_factory(source):
            raise RuntimeError("boto3 session lacks the client factory operation")
        if service_name == "sts":
            candidate = source.client("sts")
            if not _is_sts_client(candidate):
                raise RuntimeError("boto3 STS client lacks required operations")
            return candidate
        if service_name == "ec2":
            candidate = source.client("ec2")
            if not _is_ec2_client(candidate):
                raise RuntimeError("boto3 EC2 client lacks required operations")
            return candidate
        candidate = source.client("autoscaling")
        if not _is_autoscaling_client(candidate):
            raise RuntimeError("boto3 Auto Scaling client lacks required operations")
        return candidate


def _has_operations(value: object, operations: tuple[str, ...]) -> bool:
    return all(callable(getattr(value, operation, None)) for operation in operations)


def _is_sts_client(value: object) -> TypeGuard[AwsManagedPoolStsClient]:
    return _has_operations(value, ("assume_role",))


def _is_ec2_client(value: object) -> TypeGuard[AwsManagedPoolEc2Client]:
    return _has_operations(
        value,
        (
            "create_launch_template",
            "create_launch_template_version",
            "delete_launch_template",
            "describe_instances",
            "describe_launch_templates",
            "describe_launch_template_versions",
            "describe_volumes",
            "modify_launch_template",
        ),
    )


def _is_autoscaling_client(value: object) -> TypeGuard[AwsManagedPoolAutoScalingClient]:
    return _has_operations(
        value,
        (
            "create_auto_scaling_group",
            "delete_auto_scaling_group",
            "describe_auto_scaling_groups",
            "set_desired_capacity",
            "terminate_instance_in_auto_scaling_group",
            "update_auto_scaling_group",
        ),
    )


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _InstanceEbs(_Response):
    volume_id: str = Field(default="", alias="VolumeId")


class _InstanceBlockDevice(_Response):
    ebs: _InstanceEbs | None = Field(default=None, alias="Ebs")


class _InstanceState(_Response):
    name: str = Field(default="", alias="Name")


class _InstanceStorage(_Response):
    instance_id: str = Field(alias="InstanceId")
    state: _InstanceState = Field(default_factory=_InstanceState, alias="State")
    block_devices: tuple[_InstanceBlockDevice, ...] = Field(default=(), alias="BlockDeviceMappings")


class _InstanceReservation(_Response):
    instances: tuple[_InstanceStorage, ...] = Field(default=(), alias="Instances")


class _DescribeInstanceStorage(_Response):
    reservations: tuple[_InstanceReservation, ...] = Field(default=(), alias="Reservations")


class _Volume(_Response):
    volume_id: str = Field(alias="VolumeId")


class _DescribeVolumes(_Response):
    volumes: tuple[_Volume, ...] = Field(default=(), alias="Volumes")


class _Credentials(_Response):
    access_key: SecretStr = Field(alias="AccessKeyId")
    secret_key: SecretStr = Field(alias="SecretAccessKey")
    token: SecretStr = Field(alias="SessionToken")


class _AssumeResponse(_Response):
    credentials: _Credentials = Field(alias="Credentials")


@dataclass(frozen=True, slots=True)
class Boto3AwsManagedPoolClientProvider:
    session_factory: AwsManagedPoolSessionFactory

    @classmethod
    def from_default_chain(cls) -> Self:
        return cls(_default_session)

    def assume(self, target: AwsAccountConnectionTarget) -> AwsManagedPoolClients:
        target.validated_scope()
        source = self.session_factory(region_name=target.region)
        try:
            response = source.client("sts").assume_role(
                RoleArn=target.role_arn,
                RoleSessionName=_pool_session_name(target),
                ExternalId=target.external_id.get_secret_value(),
                DurationSeconds=3600,
            )
            credentials = _validate(
                _AssumeResponse, response, operation="assume account connection role"
            ).credentials
        except ClientError as exc:
            raise _client_error(exc, operation="assume account connection role") from exc
        except BotoCoreError as exc:
            raise _upstream_error(exc, operation="assume account connection role") from exc
        assumed = self.session_factory(
            region_name=target.region,
            aws_access_key_id=credentials.access_key.get_secret_value(),
            aws_secret_access_key=credentials.secret_key.get_secret_value(),
            aws_session_token=credentials.token.get_secret_value(),
        )
        return AwsManagedPoolClients(
            ec2=assumed.client("ec2"),
            autoscaling=assumed.client("autoscaling"),
        )


class _LaunchTemplate(_Response):
    id: str = Field(alias="LaunchTemplateId")
    latest_version: int = Field(alias="LatestVersionNumber")


class _LaunchTemplates(_Response):
    values: tuple[_LaunchTemplate, ...] = Field(default=(), alias="LaunchTemplates")


class _LaunchTemplateVersion(_Response):
    version: int = Field(alias="VersionNumber")
    description: str = Field(default="", alias="VersionDescription")


class _LaunchTemplateVersions(_Response):
    values: tuple[_LaunchTemplateVersion, ...] = Field(default=(), alias="LaunchTemplateVersions")


class _GroupInstance(_Response):
    instance_id: str = Field(alias="InstanceId")
    lifecycle_state: str = Field(default="", alias="LifecycleState")
    health_status: str = Field(default="", alias="HealthStatus")
    availability_zone: str = Field(default="", alias="AvailabilityZone")


class _GroupLaunchTemplate(_Response):
    id: str = Field(alias="LaunchTemplateId")
    version: str = Field(alias="Version")


class _Group(_Response):
    name: str = Field(alias="AutoScalingGroupName")
    desired: int = Field(alias="DesiredCapacity")
    minimum: int = Field(alias="MinSize")
    maximum: int = Field(alias="MaxSize")
    vpc_zone_identifier: str = Field(alias="VPCZoneIdentifier")
    launch_template: _GroupLaunchTemplate | None = Field(default=None, alias="LaunchTemplate")
    instances: tuple[_GroupInstance, ...] = Field(default=(), alias="Instances")
    tags: tuple[Mapping[str, object], ...] = Field(default=(), alias="Tags")


class _Groups(_Response):
    values: tuple[_Group, ...] = Field(default=(), alias="AutoScalingGroups")


class AwsManagedPoolProvisioner:
    def __init__(self, clients: AwsManagedPoolClients) -> None:
        self._clients = clients

    @classmethod
    def assume(
        cls,
        connection: AwsAccountConnectionTarget,
        *,
        client_provider: AwsManagedPoolClientProvider,
    ) -> AwsManagedPoolProvisioner:
        return cls(client_provider.assume(connection))

    def ensure(
        self,
        spec: AwsManagedPoolSpec,
        prior: AwsManagedPoolResourceIds | None = None,
        *,
        progress: AwsManagedPoolProgressSink | None = None,
    ) -> AwsManagedPoolSnapshot:
        state = prior or AwsManagedPoolResourceIds()

        def checkpoint(updated: AwsManagedPoolResourceIds) -> AwsManagedPoolResourceIds:
            if progress is not None:
                progress(updated)
            return updated

        try:
            launch_template_id, launch_template_version = self._ensure_launch_template(
                spec, spec.security_group_id
            )
            state = checkpoint(
                state.model_copy(
                    update={
                        "launch_template_id": launch_template_id,
                        "launch_template_version": launch_template_version,
                    }
                )
            )
            group = self._ensure_group(
                spec,
                subnets=spec.subnet_ids,
                launch_template_id=launch_template_id,
                launch_template_version=launch_template_version,
            )
            state = checkpoint(state.model_copy(update={"autoscaling_group_name": group.name}))
        except AwsProviderControlError as exc:
            raise AwsManagedPoolProvisioningError(exc, resource_ids=state) from exc
        return _snapshot(group, state)

    def describe(
        self,
        spec: AwsManagedPoolSpec,
        resource_ids: AwsManagedPoolResourceIds | None = None,
    ) -> AwsManagedPoolSnapshot:
        group = self._describe_group(spec.autoscaling_group_name)
        if group is None:
            discovered = self.discover(spec, resource_ids)
            phase = (
                AwsManagedPoolPhase.Provisioning
                if _has_resources(discovered)
                else AwsManagedPoolPhase.Deleted
            )
            return AwsManagedPoolSnapshot(
                phase=phase,
                resource_ids=discovered,
                desired_nodes=0,
                max_nodes=0,
            )
        state = self.discover(spec, resource_ids).model_copy(
            update={"autoscaling_group_name": group.name}
        )
        return _snapshot(group, state)

    def discover(
        self,
        spec: AwsManagedPoolSpec,
        prior: AwsManagedPoolResourceIds | None = None,
    ) -> AwsManagedPoolResourceIds:
        del prior
        launch_template = self._describe_launch_template(spec.launch_template_name)
        group = self._describe_group(spec.autoscaling_group_name)
        return AwsManagedPoolResourceIds(
            launch_template_id=launch_template.id if launch_template is not None else None,
            launch_template_version=(
                launch_template.latest_version if launch_template is not None else None
            ),
            autoscaling_group_name=group.name if group is not None else None,
        )

    def scale(self, spec: AwsManagedPoolSpec, *, desired_nodes: int, max_nodes: int) -> None:
        if (
            desired_nodes < 0
            or max_nodes < 1
            or desired_nodes > max_nodes
            or max_nodes > spec.max_nodes
        ):
            raise ValueError("invalid managed pool capacity")
        group = self._require_group(spec)
        resources = self.discover(spec)
        if resources.launch_template_id is None or resources.launch_template_version is None:
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.ResourceNotFound,
                operation="scale managed pool",
                detail="managed pool launch template is incomplete",
            )
        self._asg(
            "scale Auto Scaling Group",
            self._clients.autoscaling.update_auto_scaling_group,
            AutoScalingGroupName=group.name,
            MinSize=0,
            MaxSize=max_nodes,
            DesiredCapacity=desired_nodes,
            VPCZoneIdentifier=",".join(spec.subnet_ids),
            LaunchTemplate={
                "LaunchTemplateId": resources.launch_template_id,
                "Version": str(resources.launch_template_version),
            },
        )

    def release_instance(
        self, spec: AwsManagedPoolSpec, instance_id: str, *, decrement_desired: bool
    ) -> bool:
        normalized = instance_id.strip().lower()
        if not _INSTANCE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("invalid EC2 instance ID")
        group = self._require_group(spec)
        if normalized not in {instance.instance_id for instance in group.instances}:
            return False
        self._asg(
            "release managed pool instance",
            self._clients.autoscaling.terminate_instance_in_auto_scaling_group,
            InstanceId=normalized,
            ShouldDecrementDesiredCapacity=decrement_desired,
        )
        return True

    def delete(
        self,
        spec: AwsManagedPoolSpec,
        resource_ids: AwsManagedPoolResourceIds | None = None,
    ) -> AwsManagedPoolSnapshot:
        state = self.discover(spec, resource_ids)
        group = self._describe_group(spec.autoscaling_group_name)
        if group is not None:
            self._asg(
                "delete Auto Scaling Group",
                self._clients.autoscaling.delete_auto_scaling_group,
                AutoScalingGroupName=group.name,
                ForceDelete=True,
            )
            return AwsManagedPoolSnapshot(
                phase=AwsManagedPoolPhase.Deleting,
                resource_ids=state,
                desired_nodes=0,
                max_nodes=group.maximum,
                instances=tuple(
                    AwsManagedPoolInstance(
                        instance_id=item.instance_id,
                        lifecycle_state=item.lifecycle_state,
                        health_status=item.health_status,
                        availability_zone=item.availability_zone,
                    )
                    for item in group.instances
                ),
            )
        if state.launch_template_id is not None:
            self._ignore_missing(
                "delete launch template",
                self._clients.ec2.delete_launch_template,
                LaunchTemplateId=state.launch_template_id,
            )
        remaining = self.discover(spec)
        return AwsManagedPoolSnapshot(
            phase=(
                AwsManagedPoolPhase.Deleting
                if _has_resources(remaining)
                else AwsManagedPoolPhase.Deleted
            ),
            resource_ids=remaining,
            desired_nodes=0,
            max_nodes=0,
        )

    def _ensure_launch_template(
        self, spec: AwsManagedPoolSpec, security_group_id: str
    ) -> tuple[str, int]:
        data = _launch_template_data(spec, security_group_id)
        fingerprint = _launch_template_fingerprint(data)
        found = self._describe_launch_template(spec.launch_template_name)
        if found is None:
            response = self._ec2(
                "create launch template",
                self._clients.ec2.create_launch_template,
                LaunchTemplateName=spec.launch_template_name,
                VersionDescription=fingerprint,
                LaunchTemplateData=data,
                TagSpecifications=[_tag_spec("launch-template", spec, "launch-template")],
            )
            created = _validate(
                _LaunchTemplateEnvelope, response, operation="create launch template"
            ).value
            return created.id, created.latest_version
        versions = _validate(
            _LaunchTemplateVersions,
            self._ec2(
                "describe launch template version",
                self._clients.ec2.describe_launch_template_versions,
                LaunchTemplateId=found.id,
                Versions=[str(found.latest_version)],
            ),
            operation="describe launch template version",
        ).values
        if len(versions) != 1:
            raise _invalid(
                "describe launch template version",
                "AWS returned an unexpected launch template version set",
            )
        if versions[0].description == fingerprint:
            return found.id, versions[0].version
        response = self._ec2(
            "create launch template version",
            self._clients.ec2.create_launch_template_version,
            LaunchTemplateId=found.id,
            VersionDescription=fingerprint,
            LaunchTemplateData=data,
        )
        version = _validate(
            _LaunchTemplateVersionEnvelope, response, operation="create launch template version"
        ).value.version
        self._ec2(
            "set default launch template version",
            self._clients.ec2.modify_launch_template,
            LaunchTemplateId=found.id,
            DefaultVersion=str(version),
        )
        return found.id, version

    def _ensure_group(
        self,
        spec: AwsManagedPoolSpec,
        *,
        subnets: tuple[str, str],
        launch_template_id: str,
        launch_template_version: int,
    ) -> _Group:
        found = self._describe_group(spec.autoscaling_group_name)
        template: _LaunchTemplateRef = {
            "LaunchTemplateId": launch_template_id,
            "Version": str(launch_template_version),
        }
        if found is None:
            self._asg(
                "create Auto Scaling Group",
                self._clients.autoscaling.create_auto_scaling_group,
                AutoScalingGroupName=spec.autoscaling_group_name,
                MinSize=0,
                MaxSize=spec.max_nodes,
                DesiredCapacity=spec.desired_nodes,
                HealthCheckType="EC2",
                HealthCheckGracePeriod=300,
                VPCZoneIdentifier=",".join(subnets),
                LaunchTemplate=template,
                Tags=_asg_tags(spec),
            )
        else:
            _validate_group_tags(found, spec)
            expected_subnets = frozenset(subnets)
            actual_subnets = frozenset(
                subnet for subnet in found.vpc_zone_identifier.split(",") if subnet
            )
            if (
                found.minimum != 0
                or found.maximum != spec.max_nodes
                or found.desired != spec.desired_nodes
                or actual_subnets != expected_subnets
                or found.launch_template is None
                or found.launch_template.id != launch_template_id
                or found.launch_template.version != str(launch_template_version)
            ):
                self._asg(
                    "update Auto Scaling Group",
                    self._clients.autoscaling.update_auto_scaling_group,
                    AutoScalingGroupName=found.name,
                    MinSize=0,
                    MaxSize=spec.max_nodes,
                    DesiredCapacity=spec.desired_nodes,
                    VPCZoneIdentifier=",".join(subnets),
                    LaunchTemplate=template,
                )
        refreshed = self._describe_group(spec.autoscaling_group_name)
        if refreshed is None:
            raise _invalid(
                "ensure Auto Scaling Group", "AWS did not return the managed Auto Scaling Group"
            )
        return refreshed

    def _describe_group(self, name: str) -> _Group | None:
        payload = _validate(
            _Groups,
            self._asg(
                "describe Auto Scaling Group",
                self._clients.autoscaling.describe_auto_scaling_groups,
                AutoScalingGroupNames=[name],
            ),
            operation="describe Auto Scaling Group",
        )
        if len(payload.values) > 1 or (payload.values and payload.values[0].name != name):
            raise _invalid(
                "describe Auto Scaling Group",
                "AWS returned an Auto Scaling Group outside the requested scope",
            )
        return payload.values[0] if payload.values else None

    def _require_group(self, spec: AwsManagedPoolSpec) -> _Group:
        group = self._describe_group(spec.autoscaling_group_name)
        if group is None:
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.ResourceNotFound,
                operation="describe Auto Scaling Group",
                detail="managed Auto Scaling Group does not exist",
            )
        _validate_group_tags(group, spec)
        return group

    def _describe_launch_template(self, name: str) -> _LaunchTemplate | None:
        try:
            payload = _validate(
                _LaunchTemplates,
                self._ec2(
                    "describe launch template",
                    self._clients.ec2.describe_launch_templates,
                    LaunchTemplateNames=[name],
                ),
                operation="describe launch template",
            )
        except AwsProviderControlError as exc:
            if exc.code is AwsProviderControlErrorCode.ResourceNotFound:
                return None
            raise
        if len(payload.values) > 1:
            raise _invalid(
                "describe launch template", "AWS returned duplicate named launch templates"
            )
        return payload.values[0] if payload.values else None

    def storage_volume_ids(
        self,
        instance_ids: tuple[str, ...],
    ) -> dict[str, tuple[str, ...]]:
        if not instance_ids:
            return {}
        response = self._ec2(
            "describe managed pool instance storage",
            self._clients.ec2.describe_instances,
            InstanceIds=list(instance_ids),
        )
        described = _validate(
            _DescribeInstanceStorage,
            response,
            operation="describe managed pool instance storage",
        )
        return {
            instance.instance_id: tuple(
                mapping.ebs.volume_id
                for mapping in instance.block_devices
                if mapping.ebs is not None and mapping.ebs.volume_id
            )
            for reservation in described.reservations
            for instance in reservation.instances
        }

    def machine_storage_destroyed(
        self,
        spec: AwsManagedPoolSpec,
        instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        try:
            response = self._clients.ec2.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            error = exc.response.get("Error")
            code = str(error.get("Code") or "") if isinstance(error, dict) else ""
            if code != "InvalidInstanceID.NotFound":
                raise
        else:
            described = _validate(
                _DescribeInstanceStorage,
                response,
                operation="verify managed pool instance destruction",
            )
            states = [
                instance.state.name
                for reservation in described.reservations
                for instance in reservation.instances
            ]
            if states and any(state != "terminated" for state in states):
                return False
        if not storage_volume_ids:
            volume_response = self._ec2(
                "verify managed pool tagged volume destruction",
                self._clients.ec2.describe_volumes,
                Filters=_filters(spec, "volume"),
            )
            described_volumes = _validate(
                _DescribeVolumes,
                volume_response,
                operation="verify managed pool tagged volume destruction",
            )
            return not described_volumes.volumes
        for volume_id in storage_volume_ids:
            try:
                volume_response = self._clients.ec2.describe_volumes(VolumeIds=[volume_id])
            except ClientError as exc:
                error = exc.response.get("Error")
                code = str(error.get("Code") or "") if isinstance(error, dict) else ""
                if code == "InvalidVolume.NotFound":
                    continue
                raise
            described_volumes = _validate(
                _DescribeVolumes,
                volume_response,
                operation="verify managed pool volume destruction",
            )
            if described_volumes.volumes:
                return False
        return True

    def _ec2(self, operation: str, method: object, **kwargs: object) -> Mapping[str, object]:
        return _aws_call(operation, method, kwargs)

    def _asg(self, operation: str, method: object, **kwargs: object) -> Mapping[str, object]:
        return _aws_call(operation, method, kwargs)

    def _ignore_missing(self, operation: str, method: object, **kwargs: object) -> None:
        try:
            _aws_call(operation, method, kwargs)
        except AwsProviderControlError as exc:
            if exc.code is not AwsProviderControlErrorCode.ResourceNotFound:
                raise


class _LaunchTemplateEnvelope(_Response):
    value: _LaunchTemplate = Field(alias="LaunchTemplate")


class _LaunchTemplateVersionEnvelope(_Response):
    value: _LaunchTemplateVersion = Field(alias="LaunchTemplateVersion")


def _filters(spec: AwsManagedPoolSpec, resource: str) -> list[_Filter]:
    return [
        {"Name": f"tag:{AWS_MANAGED_POOL_TAG}", "Values": [AWS_MANAGED_POOL_TAG_VALUE]},
        {"Name": "tag:cloud-pool:key", "Values": [spec.resource_key]},
        {"Name": "tag:cloud-pool:resource", "Values": [resource]},
    ]


def _tags(spec: AwsManagedPoolSpec, resource: str) -> list[_Tag]:
    return [
        {"Key": "Name", "Value": f"managed-pool-{spec.pool_name}-{resource}"},
        {"Key": AWS_MANAGED_POOL_TAG, "Value": AWS_MANAGED_POOL_TAG_VALUE},
        {"Key": "cloud-pool:key", "Value": spec.resource_key},
        {"Key": "cloud-pool:workspace", "Value": spec.workspace_id},
        {"Key": "cloud-pool:name", "Value": spec.pool_name},
        {"Key": "cloud-pool:resource", "Value": resource},
    ]


def _tag_spec(resource_type: str, spec: AwsManagedPoolSpec, resource: str) -> _TagSpecification:
    return {"ResourceType": resource_type, "Tags": _tags(spec, resource)}


def _asg_tags(spec: AwsManagedPoolSpec) -> list[Mapping[str, object]]:
    return [
        {"Key": tag["Key"], "Value": tag["Value"], "PropagateAtLaunch": True}
        for tag in _tags(spec, "autoscaling-group")
    ]


def _launch_template_data(spec: AwsManagedPoolSpec, security_group_id: str) -> _LaunchTemplateData:
    instance_tags: list[_Tag] = [
        *_tags(spec, "instance"),
        {"Key": "cloud-pool:enrollment", "Value": spec.bootstrap.enrollment_request_id},
    ]
    return {
        "ImageId": spec.ami_id,
        "InstanceType": spec.instance_type,
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": spec.root_volume_gib,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "IamInstanceProfile": {"Arn": spec.node_instance_profile_arn},
        "SecurityGroupIds": [security_group_id],
        "MetadataOptions": {
            "HttpEndpoint": "enabled",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
            "InstanceMetadataTags": "enabled",
        },
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": instance_tags},
            {"ResourceType": "volume", "Tags": _tags(spec, "volume")},
        ],
        "UserData": base64.b64encode(aws_managed_pool_bootstrap_script(spec).encode()).decode(),
    }


_BOOTSTRAP_SCRIPT_TEMPLATE = """#!/bin/bash
set -Eeuo pipefail

CONTROL_PLANE_URL=__CONTROL_PLANE_URL__
ENROLLMENT_REQUEST_ID=__ENROLLMENT_REQUEST_ID__
AGENT_SHA256=__AGENT_SHA256__
AGENT_ARTIFACT_URL=__AGENT_ARTIFACT_URL__
WORKER_IMAGE_DIGEST=__WORKER_IMAGE_DIGEST__
GPU_COUNT=__GPU_COUNT__
TAILSCALE_VERSION=__TAILSCALE_VERSION__
TAILSCALE_SHA256=__TAILSCALE_SHA256__
AGENT_BIN=__AGENT_BIN__
AGENT_STATE_DIR=__AGENT_STATE_DIR__
EMPTY_PAYLOAD_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

STEP=identity
IMDS_TOKEN=""
INSTANCE_ID=""
REGION=""
STS_HOST=""
AWS_ACCESS_KEY_ID=""
AWS_SECRET_ACCESS_KEY=""
AWS_SESSION_TOKEN=""

bootstrap_failed() {
  status=$?
  trap - ERR
  reason=unknown
  case "$STEP" in
    docker) reason=runtime_install_failed ;;
    tailscale) reason=network_join_failed ;;
    agent) reason=agent_download_failed ;;
  esac
  report_failure "$reason"
  echo "worker bootstrap failed during ${STEP}; leaving instance available for inspection" >&2
  exit "$status"
}
trap bootstrap_failed ERR

bootstrap_error() {
  echo "error: $1" >&2
  return 1
}

imds() {
  curl -fsS --retry 5 --retry-delay 2 \\
    -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" \\
    "http://169.254.169.254/latest/$1"
}

sha256_hex() {
  printf '%s' "$1" | openssl dgst -sha256 | awk '{print $NF}'
}

hmac_hex() {
  printf '%s' "$2" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$1" | awk '{print $NF}'
}

uri_encode() {
  raw="$1"
  encoded=""
  while [ -n "$raw" ]; do
    char="${raw%"${raw#?}"}"
    raw="${raw#?}"
    case "$char" in
      [A-Za-z0-9.~_-]) encoded="$encoded$char" ;;
      *) encoded="$encoded$(printf '%%%02X' "'$char")" ;;
    esac
  done
  printf '%s' "$encoded"
}

# SigV4-presigned STS GetCallerIdentity URL minted with curl-fetched instance
# role credentials and openssl only. AMZ_DATE overrides the signing time for
# deterministic testing.
mint_proof() {
  amz_date="${AMZ_DATE:-$(date -u +%Y%m%dT%H%M%SZ)}"
  date_stamp="${amz_date%%T*}"
  scope="${date_stamp}/${REGION}/sts/aws4_request"
  query="Action=GetCallerIdentity"
  query="${query}&Version=2011-06-15"
  query="${query}&X-Amz-Algorithm=AWS4-HMAC-SHA256"
  query="${query}&X-Amz-Credential=$(uri_encode "${AWS_ACCESS_KEY_ID}/${scope}")"
  query="${query}&X-Amz-Date=${amz_date}"
  query="${query}&X-Amz-Expires=30"
  query="${query}&X-Amz-Security-Token=$(uri_encode "$AWS_SESSION_TOKEN")"
  query="${query}&X-Amz-SignedHeaders=host"
  canonical_request="GET
/
${query}
host:${STS_HOST}

host
${EMPTY_PAYLOAD_SHA256}"
  string_to_sign="AWS4-HMAC-SHA256
${amz_date}
${scope}
$(sha256_hex "$canonical_request")"
  date_key=$(printf '%s' "$date_stamp" | openssl dgst -sha256 -mac HMAC \\
    -macopt "key:AWS4${AWS_SECRET_ACCESS_KEY}" | awk '{print $NF}')
  region_key=$(hmac_hex "$date_key" "$REGION")
  service_key=$(hmac_hex "$region_key" "sts")
  signing_key=$(hmac_hex "$service_key" "aws4_request")
  signature=$(hmac_hex "$signing_key" "$string_to_sign")
  printf 'https://%s/?%s&X-Amz-Signature=%s' "$STS_HOST" "$query" "$signature"
}

# Bootstrap reports authenticate with a fresh single-use identity proof per
# call and never abort the boot flow.
report() {
  endpoint="$1"
  field="$2"
  value="$3"
  proof="$(mint_proof)"
  payload="{\\"enrollment_request_id\\":\\"${ENROLLMENT_REQUEST_ID}\\""
  payload="${payload},\\"provider\\":\\"aws\\""
  payload="${payload},\\"region\\":\\"${REGION}\\""
  payload="${payload},\\"provider_instance_id\\":\\"${INSTANCE_ID}\\""
  payload="${payload},\\"identity_proof_url\\":\\"${proof}\\""
  payload="${payload},\\"${field}\\":\\"${value}\\"}"
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 -X POST \\
    -H 'Content-Type: application/json' \\
    --data "$payload" \\
    "${CONTROL_PLANE_URL}/gateway/provider-nodes/${endpoint}" >/dev/null
}

report_phase() {
  report bootstrap-phase phase "$1" || true
}

report_failure() {
  report bootstrap-failure failure_reason "$1" || true
}

docker_ready() {
  docker info >/dev/null 2>&1
}

tailscale_ready() {
  command -v tailscale >/dev/null 2>&1 && \\
    command -v tailscaled >/dev/null 2>&1 && \\
    [ "$(tailscale version 2>/dev/null | sed -n 1p)" = "$TAILSCALE_VERSION" ] && \\
    [ "$(tailscaled --version 2>/dev/null | sed -n 1p)" = "$TAILSCALE_VERSION" ]
}

agent_ready() {
  [ -x "$AGENT_BIN" ] && \\
    [ "$(sha256sum "$AGENT_BIN" 2>/dev/null | awk '{print $1}')" = "$AGENT_SHA256" ]
}

ensure_docker() {
  if docker_ready; then
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    dnf install -y docker
  fi
  systemctl enable --now docker
  for _ in {1..30}; do
    if docker_ready; then
      return
    fi
    sleep 2
  done
  bootstrap_error 'Docker daemon did not become ready'
}

ensure_tailscale() {
  if tailscale_ready; then
    return
  fi
  archive=$(mktemp)
  extracted=$(mktemp -d)
  curl -fsSL --retry 5 --retry-delay 2 \\
    "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz" \\
    -o "$archive"
  if [ "$(sha256sum "$archive" | awk '{print $1}')" != "$TAILSCALE_SHA256" ]; then
    rm -rf "$archive" "$extracted"
    bootstrap_error 'Tailscale archive SHA-256 mismatch'
  fi
  tar -xzf "$archive" -C "$extracted"
  release_dir="${extracted}/tailscale_${TAILSCALE_VERSION}_amd64"
  install -m 0755 "$release_dir/tailscale" /usr/local/bin/tailscale
  install -m 0755 "$release_dir/tailscaled" /usr/local/bin/tailscaled
  rm -rf "$archive" "$extracted"
  if ! tailscale_ready; then
    bootstrap_error 'Tailscale failed its pinned version readiness check'
  fi
}

ensure_agent() {
  if agent_ready; then
    return
  fi
  agent_download=$(mktemp "${AGENT_BIN}.download.XXXXXX")
  curl -fsSL --retry 5 --retry-delay 2 "$AGENT_ARTIFACT_URL" -o "$agent_download"
  if [ "$(sha256sum "$agent_download" | awk '{print $1}')" != "$AGENT_SHA256" ]; then
    rm -f "$agent_download"
    bootstrap_error 'agent artifact SHA-256 mismatch'
  fi
  chmod 0755 "$agent_download"
  mv -f "$agent_download" "$AGENT_BIN"
}

bootstrap_main() {
  install -d -m 0700 "$AGENT_STATE_DIR"
  IMDS_TOKEN=$(curl -fsS --retry 5 --retry-delay 2 -X PUT \\
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \\
    http://169.254.169.254/latest/api/token)
  INSTANCE_ID=$(imds meta-data/instance-id)
  REGION=$(imds meta-data/placement/region)
  case "$REGION" in
    cn-*) STS_HOST="sts.${REGION}.amazonaws.com.cn" ;;
    *) STS_HOST="sts.${REGION}.amazonaws.com" ;;
  esac
  role_name=$(imds meta-data/iam/security-credentials/)
  credentials=$(imds "meta-data/iam/security-credentials/${role_name}")
  AWS_ACCESS_KEY_ID=$(printf '%s' "$credentials" |
    sed -n 's/.*"AccessKeyId"[^"]*"\\([^"]*\\)".*/\\1/p')
  AWS_SECRET_ACCESS_KEY=$(printf '%s' "$credentials" |
    sed -n 's/.*"SecretAccessKey"[^"]*"\\([^"]*\\)".*/\\1/p')
  AWS_SESSION_TOKEN=$(printf '%s' "$credentials" |
    sed -n 's/.*"Token"[^"]*"\\([^"]*\\)".*/\\1/p')
  if [ -z "$AWS_ACCESS_KEY_ID" ] || [ -z "$AWS_SECRET_ACCESS_KEY" ] ||
    [ -z "$AWS_SESSION_TOKEN" ]; then
    bootstrap_error 'instance role credentials are unavailable from IMDS'
  fi

  report_phase booting

  STEP=docker
  ensure_docker
  STEP=tailscale
  ensure_tailscale
  STEP=agent
  ensure_agent

  report_phase joining
  # The agent must outlive cloud-init. Running it as a cloud-init child leaves
  # the machine with no agent once that script module exits: it enrolls once,
  # registers a worker, then disappears, so the worker never leaves `pending`.
  #
  # The agent installs its own unit. Writing one here too would make this script
  # a second owner of the same file, and the two silently disagreed: a machine
  # was found running the agent's unit while this script claimed a different
  # restart policy, so a fix applied here never reached any machine.
  "$AGENT_BIN" install-service \\
    --gateway "$CONTROL_PLANE_URL" \\
    --provider-enrollment-request "$ENROLLMENT_REQUEST_ID" \\
    --provider aws \\
    --provider-instance-identity imds-v2 \\
    --machine-fingerprint "$INSTANCE_ID" \\
    --hostname "$INSTANCE_ID" \\
    --executor container \\
    --worker-image "$WORKER_IMAGE_DIGEST" \\
    --max-gpus "$GPU_COUNT" \\
    --state-dir "$AGENT_STATE_DIR"
}

bootstrap_main
"""


def aws_managed_pool_bootstrap_script(spec: AwsManagedPoolSpec) -> str:
    bootstrap = spec.bootstrap
    values = {
        "__CONTROL_PLANE_URL__": bootstrap.control_plane_url,
        "__ENROLLMENT_REQUEST_ID__": bootstrap.enrollment_request_id,
        "__AGENT_SHA256__": bootstrap.agent_sha256,
        "__AGENT_ARTIFACT_URL__": bootstrap.agent_artifact_url,
        "__WORKER_IMAGE_DIGEST__": bootstrap.worker_image_digest,
        "__GPU_COUNT__": str(bootstrap.gpu_count),
        "__TAILSCALE_VERSION__": TAILSCALE_INSTALL_VERSION,
        "__TAILSCALE_SHA256__": TAILSCALE_AMD64_SHA256,
        "__AGENT_BIN__": f"/usr/local/bin/{AGENT_NAME}",
        "__AGENT_STATE_DIR__": f"{STATE_DIR}/agent",
    }
    script = _BOOTSTRAP_SCRIPT_TEMPLATE
    for placeholder, value in values.items():
        script = script.replace(placeholder, shlex.quote(value))
    return script


def _launch_template_fingerprint(data: _LaunchTemplateData) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return f"managed-{hashlib.sha256(payload).hexdigest()[:48]}"


def _validate_group_tags(group: _Group, spec: AwsManagedPoolSpec) -> None:
    tags: dict[str, str] = {}
    for raw in group.tags:
        key = raw.get("Key")
        value = raw.get("Value")
        if isinstance(key, str) and isinstance(value, str):
            tags[key] = value
    if (
        tags.get(AWS_MANAGED_POOL_TAG) != AWS_MANAGED_POOL_TAG_VALUE
        or tags.get("cloud-pool:key") != spec.resource_key
    ):
        raise _invalid(
            "validate Auto Scaling Group ownership",
            "named Auto Scaling Group is not owned by this pool",
        )


def _snapshot(
    group: _Group,
    state: AwsManagedPoolResourceIds,
) -> AwsManagedPoolSnapshot:
    instances = tuple(
        AwsManagedPoolInstance(
            instance_id=item.instance_id,
            lifecycle_state=item.lifecycle_state,
            health_status=item.health_status,
            availability_zone=item.availability_zone,
        )
        for item in group.instances
    )
    ready_nodes = sum(
        instance.lifecycle_state == "InService" and instance.health_status == "Healthy"
        for instance in instances
    )
    phase = (
        AwsManagedPoolPhase.Ready
        if state.complete and ready_nodes >= group.desired
        else AwsManagedPoolPhase.Provisioning
    )
    return AwsManagedPoolSnapshot(
        phase=phase,
        resource_ids=state,
        desired_nodes=group.desired,
        max_nodes=group.maximum,
        instances=instances,
    )


def _has_resources(state: AwsManagedPoolResourceIds) -> bool:
    return any(
        (
            state.launch_template_id,
            state.autoscaling_group_name,
        )
    )


def _pool_session_name(target: AwsAccountConnectionTarget) -> str:
    digest = hashlib.sha256(f"{target.account_id}\0{target.role_arn}".encode()).hexdigest()[:16]
    return f"managed-pool-{digest}"


def _validate[ResponseT: BaseModel](
    model: type[ResponseT], response: Mapping[str, object], *, operation: str
) -> ResponseT:
    try:
        return model.model_validate(response)
    except ValidationError as exc:
        raise _invalid(operation, "AWS returned an invalid response") from exc


def _invalid(operation: str, detail: str) -> AwsProviderControlError:
    return AwsProviderControlError(
        AwsProviderControlErrorCode.InvalidResponse, operation=operation, detail=detail
    )


def _client_error_code(exc: ClientError) -> str:
    raw = exc.response.get("Error", {})
    return str(raw.get("Code", "")) if isinstance(raw, Mapping) else ""


def _client_error(exc: ClientError, *, operation: str) -> AwsProviderControlError:
    raw = exc.response.get("Error", {})
    code = _client_error_code(exc)
    message = str(raw.get("Message", "")) if isinstance(raw, Mapping) else ""
    normalized = code.casefold()
    if normalized in {"accessdenied", "accessdeniedexception", "unauthorizedoperation"}:
        category = AwsProviderControlErrorCode.PermissionDenied
    elif (
        normalized
        in {
            "invalidgroup.notfound",
            "invalidvpcid.notfound",
            "invalidsubnetid.notfound",
            "invalidroutetableid.notfound",
            "invalidlaunchtemplateid.notfound",
            "resourcecontentionfault",
        }
        or "notfound" in normalized
    ):
        category = AwsProviderControlErrorCode.ResourceNotFound
    elif normalized in {
        "requestlimitexceeded",
        "serviceunavailable",
        "throttling",
        "throttlingexception",
    }:
        category = AwsProviderControlErrorCode.UpstreamUnavailable
    else:
        category = AwsProviderControlErrorCode.UpstreamUnavailable
    return AwsProviderControlError(
        category,
        operation=operation,
        detail=message.strip() or code.strip() or "AWS request failed",
    )


def _upstream_error(exc: BotoCoreError, *, operation: str) -> AwsProviderControlError:
    return AwsProviderControlError(
        AwsProviderControlErrorCode.UpstreamUnavailable, operation=operation, detail=str(exc)
    )


def _aws_call(
    operation: str,
    method: object,
    kwargs: Mapping[str, object],
) -> Mapping[str, object]:
    if not callable(method):
        raise TypeError("AWS client operation is not callable")
    try:
        response = method(**kwargs)
    except ClientError as exc:
        raise _client_error(exc, operation=operation) from exc
    except BotoCoreError as exc:
        raise _upstream_error(exc, operation=operation) from exc
    if not isinstance(response, Mapping):
        raise _invalid(operation, "AWS returned a non-object response")
    try:
        return _AWS_RESPONSE.validate_python(response)
    except ValidationError as exc:
        raise _invalid(operation, "AWS returned an unsupported response value") from exc


def _default_session(
    *,
    region_name: str,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_session_token: str | None = None,
) -> AwsManagedPoolSession:
    return _Boto3ManagedPoolSession(
        Session(
            region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
    )

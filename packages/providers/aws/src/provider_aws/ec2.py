from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, runtime_checkable

from compute.bootstrap import MachineBootstrapConfig, machine_bootstrap_user_data_base64
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from shared.app_identity import CLUSTER_NAME_LABEL, MACHINE_ID_LABEL, POOL_NAME_LABEL


class AwsEc2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _AwsEc2ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


type AwsEc2ResponseValue = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | datetime
    | list[AwsEc2ResponseValue]
    | dict[str, AwsEc2ResponseValue]
)
type AwsEc2Response = Mapping[str, AwsEc2ResponseValue]


@runtime_checkable
class AwsEc2Paginator(Protocol):
    def paginate(self, **kwargs: JsonValue) -> Iterable[AwsEc2Response]: ...


@runtime_checkable
class AwsEc2Client(Protocol):
    def run_instances(self, **kwargs: JsonValue) -> AwsEc2Response: ...

    def get_paginator(self, operation_name: str) -> AwsEc2Paginator: ...

    def terminate_instances(self, **kwargs: JsonValue) -> AwsEc2Response | None: ...

    def describe_instances(self, **kwargs: JsonValue) -> AwsEc2Response: ...

    def describe_volumes(self, **kwargs: JsonValue) -> AwsEc2Response: ...

    def describe_regions(self, **kwargs: JsonValue) -> AwsEc2Response: ...


class AwsEc2InstanceState(StrEnum):
    Pending = "pending"
    Running = "running"


class AwsEc2TagKey(StrEnum):
    Name = "Name"
    ClusterName = CLUSTER_NAME_LABEL
    PoolName = POOL_NAME_LABEL
    MachineId = MACHINE_ID_LABEL


class AwsInstanceSpec(AwsEc2Model):
    cpu_millicores: int
    memory_mb: int
    gpu: str = ""
    gpu_count: int = 0


class AwsInstanceOffer(AwsEc2Model):
    instance_type: str
    spec: AwsInstanceSpec


class AwsComputeRequest(AwsEc2Model):
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpu: str = ""
    gpu_count: int = 0


class AwsMachineUserData(AwsEc2Model):
    registration_token: str
    machine_id: str
    gateway_url: str
    install_nvidia_runtime: bool = True


class AwsEc2MachineProvisionPlan(AwsEc2Model):
    machine_id: str
    idempotency_key: str
    pool_name: str
    instance_type: str
    image_id: str
    subnet_id: str | None = None
    user_data_base64: str
    tags: dict[str, str]
    root_volume_gib: int = 200
    # Must match the AMI's own root device or EC2 silently attaches the sized
    # volume as an extra disk and leaves the instance on the AMI default size.
    root_device_name: str = "/dev/xvda"

    @property
    def client_token(self) -> str:
        identity = "\0".join(
            (
                self.tags.get(AwsEc2TagKey.ClusterName.value, ""),
                self.pool_name,
                self.idempotency_key,
            )
        )
        return sha256(identity.encode()).hexdigest()

    def run_instances_kwargs(self) -> dict[str, JsonValue]:
        kwargs: dict[str, JsonValue] = {
            "ImageId": self.image_id,
            "InstanceType": self.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": self.user_data_base64,
            "ClientToken": self.client_token,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": key, "Value": value} for key, value in sorted(self.tags.items())
                    ],
                }
            ],
            "BlockDeviceMappings": [
                {
                    "DeviceName": self.root_device_name,
                    "Ebs": {
                        "DeleteOnTermination": True,
                        "Encrypted": True,
                        "VolumeSize": self.root_volume_gib,
                        "VolumeType": "gp3",
                    },
                }
            ],
        }
        if self.subnet_id:
            kwargs["SubnetId"] = self.subnet_id
        return kwargs


class AwsEc2MachineDiscoveryPlan(AwsEc2Model):
    cluster_name: str
    pool_name: str
    states: list[AwsEc2InstanceState] = Field(
        default_factory=lambda: [
            AwsEc2InstanceState.Pending,
            AwsEc2InstanceState.Running,
        ]
    )

    def describe_instances_kwargs(self) -> dict[str, JsonValue]:
        return {
            "Filters": [
                {
                    "Name": f"tag:{AwsEc2TagKey.ClusterName.value}",
                    "Values": [self.cluster_name],
                },
                {
                    "Name": f"tag:{AwsEc2TagKey.PoolName.value}",
                    "Values": [self.pool_name],
                },
                {
                    "Name": "instance-state-name",
                    "Values": [state.value for state in self.states],
                },
            ]
        }


class AwsEc2MachineReference(AwsEc2Model):
    machine_id: str
    instance_id: str
    storage_volume_ids: tuple[str, ...] = ()


class AwsEc2MachineTerminationPlan(AwsEc2Model):
    instance_id: str

    def terminate_instances_kwargs(self) -> dict[str, JsonValue]:
        return {"InstanceIds": [self.instance_id]}


DEFAULT_EC2_INSTANCE_OFFERS: tuple[AwsInstanceOffer, ...] = (
    AwsInstanceOffer(
        instance_type="g4dn.xlarge",
        spec=AwsInstanceSpec(cpu_millicores=4_000, memory_mb=16_384, gpu="T4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.2xlarge",
        spec=AwsInstanceSpec(cpu_millicores=8_000, memory_mb=32_768, gpu="T4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.4xlarge",
        spec=AwsInstanceSpec(cpu_millicores=16_000, memory_mb=65_536, gpu="T4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.8xlarge",
        spec=AwsInstanceSpec(cpu_millicores=32_000, memory_mb=131_072, gpu="T4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.12xlarge",
        spec=AwsInstanceSpec(cpu_millicores=48_000, memory_mb=196_608, gpu="T4", gpu_count=4),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.16xlarge",
        spec=AwsInstanceSpec(cpu_millicores=64_000, memory_mb=262_144, gpu="T4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g4dn.metal",
        spec=AwsInstanceSpec(cpu_millicores=96_000, memory_mb=393_216, gpu="T4", gpu_count=8),
    ),
    AwsInstanceOffer(
        instance_type="g5.xlarge",
        spec=AwsInstanceSpec(cpu_millicores=4_000, memory_mb=16_384, gpu="A10G", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g5.2xlarge",
        spec=AwsInstanceSpec(cpu_millicores=8_000, memory_mb=32_768, gpu="A10G", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g5.4xlarge",
        spec=AwsInstanceSpec(cpu_millicores=16_000, memory_mb=65_536, gpu="A10G", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g5.8xlarge",
        spec=AwsInstanceSpec(cpu_millicores=32_000, memory_mb=131_072, gpu="A10G", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g5.16xlarge",
        spec=AwsInstanceSpec(cpu_millicores=64_000, memory_mb=262_144, gpu="A10G", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g6.xlarge",
        spec=AwsInstanceSpec(cpu_millicores=4_000, memory_mb=16_384, gpu="L4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g6.2xlarge",
        spec=AwsInstanceSpec(cpu_millicores=8_000, memory_mb=32_768, gpu="L4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g6.4xlarge",
        spec=AwsInstanceSpec(cpu_millicores=16_000, memory_mb=65_536, gpu="L4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g6.8xlarge",
        spec=AwsInstanceSpec(cpu_millicores=32_000, memory_mb=131_072, gpu="L4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="g6.16xlarge",
        spec=AwsInstanceSpec(cpu_millicores=64_000, memory_mb=262_144, gpu="L4", gpu_count=1),
    ),
    AwsInstanceOffer(
        instance_type="m6i.large",
        spec=AwsInstanceSpec(cpu_millicores=2_000, memory_mb=8_192),
    ),
    AwsInstanceOffer(
        instance_type="m6i.xlarge",
        spec=AwsInstanceSpec(cpu_millicores=4_000, memory_mb=16_384),
    ),
    AwsInstanceOffer(
        instance_type="m6i.2xlarge",
        spec=AwsInstanceSpec(cpu_millicores=8_000, memory_mb=32_768),
    ),
    AwsInstanceOffer(
        instance_type="m6i.4xlarge",
        spec=AwsInstanceSpec(cpu_millicores=16_000, memory_mb=65_536),
    ),
    AwsInstanceOffer(
        instance_type="m6i.8xlarge",
        spec=AwsInstanceSpec(cpu_millicores=32_000, memory_mb=131_072),
    ),
    AwsInstanceOffer(
        instance_type="m6i.16xlarge",
        spec=AwsInstanceSpec(cpu_millicores=64_000, memory_mb=262_144),
    ),
    AwsInstanceOffer(
        instance_type="m7i.large",
        spec=AwsInstanceSpec(cpu_millicores=2_000, memory_mb=8_192),
    ),
    AwsInstanceOffer(
        instance_type="m7i.xlarge",
        spec=AwsInstanceSpec(cpu_millicores=4_000, memory_mb=16_384),
    ),
    AwsInstanceOffer(
        instance_type="m7i.2xlarge",
        spec=AwsInstanceSpec(cpu_millicores=8_000, memory_mb=32_768),
    ),
    AwsInstanceOffer(
        instance_type="m7i.4xlarge",
        spec=AwsInstanceSpec(cpu_millicores=16_000, memory_mb=65_536),
    ),
    AwsInstanceOffer(
        instance_type="m7i.8xlarge",
        spec=AwsInstanceSpec(cpu_millicores=32_000, memory_mb=131_072),
    ),
    AwsInstanceOffer(
        instance_type="m7i.12xlarge",
        spec=AwsInstanceSpec(cpu_millicores=48_000, memory_mb=196_608),
    ),
    AwsInstanceOffer(
        instance_type="m7i.16xlarge",
        spec=AwsInstanceSpec(cpu_millicores=64_000, memory_mb=262_144),
    ),
)


def select_instance_offer(
    request: AwsComputeRequest,
    *,
    offers: Sequence[AwsInstanceOffer] | None = None,
) -> AwsInstanceOffer:
    buffered_cpu = int(request.cpu_millicores * 1.10)
    buffered_memory = int(request.memory_mb * 1.10)
    for offer in offers or DEFAULT_EC2_INSTANCE_OFFERS:
        if offer.spec.cpu_millicores < buffered_cpu:
            continue
        if offer.spec.memory_mb < buffered_memory:
            continue
        if offer.spec.gpu != request.gpu:
            continue
        if offer.spec.gpu_count < request.gpu_count:
            continue
        return offer
    msg = (
        "no EC2 instance type satisfies "
        f"cpu={request.cpu_millicores} memory={request.memory_mb} "
        f"gpu={request.gpu} gpu_count={request.gpu_count}"
    )
    raise ValueError(msg)


def encode_machine_user_data(config: AwsMachineUserData) -> str:
    return machine_bootstrap_user_data_base64(
        MachineBootstrapConfig(
            registration_token=config.registration_token,
            machine_id=config.machine_id,
            gateway_url=config.gateway_url,
            install_nvidia_runtime=config.install_nvidia_runtime,
        )
    )


class _AwsEc2Tag(_AwsEc2ResponseModel):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class _AwsEc2State(_AwsEc2ResponseModel):
    name: str = Field(default="", alias="Name")


class _AwsEc2EbsAttachment(_AwsEc2ResponseModel):
    volume_id: str = Field(default="", alias="VolumeId")


class _AwsEc2BlockDeviceMapping(_AwsEc2ResponseModel):
    ebs: _AwsEc2EbsAttachment | None = Field(default=None, alias="Ebs")


class _AwsEc2Instance(_AwsEc2ResponseModel):
    instance_id: str | None = Field(default=None, alias="InstanceId")
    tags: tuple[_AwsEc2Tag, ...] = Field(default=(), alias="Tags")
    state: _AwsEc2State = Field(default_factory=_AwsEc2State, alias="State")
    block_device_mappings: tuple[_AwsEc2BlockDeviceMapping, ...] = Field(
        default=(), alias="BlockDeviceMappings"
    )


class _AwsEc2Reservation(_AwsEc2ResponseModel):
    instances: tuple[_AwsEc2Instance, ...] = Field(default=(), alias="Instances")


class _AwsEc2RunInstancesResponse(_AwsEc2ResponseModel):
    instances: tuple[_AwsEc2Instance, ...] = Field(default=(), alias="Instances")


class _AwsEc2DescribeInstancesResponse(_AwsEc2ResponseModel):
    reservations: tuple[_AwsEc2Reservation, ...] = Field(default=(), alias="Reservations")


def extract_first_instance_id(result: AwsEc2Response) -> str:
    response = _AwsEc2RunInstancesResponse.model_validate(result)
    if response.instances and response.instances[0].instance_id:
        return response.instances[0].instance_id
    msg = "EC2 run_instances did not return an instance id"
    raise RuntimeError(msg)


def extract_first_instance_reference(result: AwsEc2Response) -> tuple[str, tuple[str, ...]]:
    response = _AwsEc2RunInstancesResponse.model_validate(result)
    if not response.instances:
        msg = "EC2 run_instances did not return an instance id"
        raise RuntimeError(msg)
    instance = response.instances[0]
    instance_id = instance.instance_id
    if not instance_id:
        msg = "EC2 run_instances did not return an instance id"
        raise RuntimeError(msg)
    return instance_id, instance_volume_ids(instance)


def describe_instance_pages(
    client: AwsEc2Client,
    kwargs: dict[str, JsonValue],
) -> list[AwsEc2Response]:
    paginator = client.get_paginator("describe_instances")
    return list(paginator.paginate(**kwargs))


def machine_references_from_describe(
    pages: Sequence[AwsEc2Response],
) -> list[AwsEc2MachineReference]:
    machines: list[AwsEc2MachineReference] = []
    for page in pages:
        response = _AwsEc2DescribeInstancesResponse.model_validate(page)
        for reservation in response.reservations:
            for instance in reservation.instances:
                machine = machine_reference_from_instance(instance)
                if machine is not None:
                    machines.append(machine)
    return machines


def instance_states_from_describe(response: AwsEc2Response) -> tuple[str, ...]:
    described = _AwsEc2DescribeInstancesResponse.model_validate(response)
    return tuple(
        instance.state.name
        for reservation in described.reservations
        for instance in reservation.instances
    )


def instance_volume_ids_from_describe(
    response: AwsEc2Response,
    instance_id: str,
) -> tuple[str, ...]:
    described = _AwsEc2DescribeInstancesResponse.model_validate(response)
    for reservation in described.reservations:
        for instance in reservation.instances:
            if instance.instance_id == instance_id:
                return instance_volume_ids(instance)
    return ()


def instance_volume_ids(instance: _AwsEc2Instance) -> tuple[str, ...]:
    return tuple(
        mapping.ebs.volume_id
        for mapping in instance.block_device_mappings
        if mapping.ebs is not None and mapping.ebs.volume_id
    )


def machine_reference_from_instance(
    instance: _AwsEc2Instance,
) -> AwsEc2MachineReference | None:
    if not instance.instance_id:
        return None
    machine_id = tag_value(instance.tags, AwsEc2TagKey.MachineId)
    if not machine_id:
        return None
    return AwsEc2MachineReference(
        machine_id=machine_id,
        instance_id=instance.instance_id,
        storage_volume_ids=instance_volume_ids(instance),
    )


def tag_value(tags: Sequence[_AwsEc2Tag], key: AwsEc2TagKey) -> str:
    for tag in tags:
        if tag.key == key.value:
            return tag.value
    return ""


__all__ = [
    "DEFAULT_EC2_INSTANCE_OFFERS",
    "AwsComputeRequest",
    "AwsEc2Client",
    "AwsEc2InstanceState",
    "AwsEc2MachineDiscoveryPlan",
    "AwsEc2MachineProvisionPlan",
    "AwsEc2MachineReference",
    "AwsEc2MachineTerminationPlan",
    "AwsEc2Response",
    "AwsEc2ResponseValue",
    "AwsEc2TagKey",
    "AwsInstanceOffer",
    "AwsInstanceSpec",
    "AwsMachineUserData",
    "describe_instance_pages",
    "encode_machine_user_data",
    "extract_first_instance_id",
    "extract_first_instance_reference",
    "instance_states_from_describe",
    "instance_volume_ids_from_describe",
    "machine_reference_from_instance",
    "machine_references_from_describe",
    "select_instance_offer",
    "tag_value",
]

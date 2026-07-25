from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from provider_aws import (
    AwsComputeRequest,
    AwsEc2TagKey,
    AwsProvider,
    AwsProviderSettings,
)
from provider_aws.ec2 import AwsEc2Response
from pydantic import JsonValue
from shared.app_identity import NAME


def test_aws_ec2_provision_machine_creates_atomically_tagged_idempotent_instance() -> None:
    provider = AwsProvider(
        AwsProviderSettings(
            region="us-west-2",
            ec2_ami="ami-123",
            cluster_name=f"{NAME}-prod",
            gateway_url="https://control.example.com",
        )
    )
    plan = provider.provision_machine_plan(
        pool_name="default",
        registration_token="join-token",
        compute=AwsComputeRequest(cpu_millicores=1_000, memory_mb=1_024),
        machine_id="machine123",
        subnet_id="subnet-1",
    )
    client = FakeEc2Client()

    instance_id = provider.provision_machine(plan, ec2_client=client)

    assert instance_id == "i-123"
    assert client.run_instances_kwargs is not None
    assert client.run_instances_kwargs["ClientToken"] == plan.client_token
    assert client.run_instances_kwargs["TagSpecifications"]


def test_aws_ec2_health_and_reconcile() -> None:
    provider = AwsProvider(AwsProviderSettings(region="us-west-2", cluster_name=f"{NAME}-prod"))
    client = FakeEc2Client()

    health = provider.health(ec2_client=client)
    reconcile = provider.reconcile_machines(
        "default",
        {"machine123", "missing"},
        ec2_client=client,
    )

    assert health.status.value == "ready"
    assert reconcile.missing_machine_ids == ["missing"]
    assert reconcile.stale_machine_ids == []
    assert [item.provider_instance_id for item in reconcile.observed_machines] == ["i-123"]


def test_aws_ec2_surviving_instance_or_volume_is_never_destroyed_storage() -> None:
    provider = AwsProvider(AwsProviderSettings(region="us-west-2"))
    live_instance = FakeEc2Client()
    live_instance.instance_state = "shutting-down"

    assert not provider.machine_storage_destroyed("i-123", ("vol-123",), ec2_client=live_instance)

    surviving_volume = FakeEc2Client()
    surviving_volume.instance_state = "terminated"

    assert not provider.machine_storage_destroyed(
        "i-123", ("vol-123",), ec2_client=surviving_volume
    )


def test_aws_ec2_not_found_is_authoritative_machine_storage_absence() -> None:
    provider = AwsProvider(AwsProviderSettings(region="us-west-2"))
    client = FakeEc2Client()
    client.describe_instances_error = ClientError(
        {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "missing"}},
        "DescribeInstances",
    )

    client.volume_missing = True
    assert provider.machine_storage_destroyed("i-gone", ("vol-gone",), ec2_client=client)

    client.describe_instances_error = ClientError(
        {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
        "DescribeInstances",
    )
    with pytest.raises(ClientError):
        provider.machine_storage_destroyed("i-unknown", ("vol-unknown",), ec2_client=client)


class FakeEc2Client:
    def __init__(self) -> None:
        self.run_instances_kwargs: dict[str, JsonValue] | None = None
        self.terminate_instances_kwargs: dict[str, JsonValue] | None = None
        self.describe_regions_kwargs: dict[str, JsonValue] | None = None
        self.describe_instances_kwargs: dict[str, JsonValue] | None = None
        self.instance_state = "shutting-down"
        self.describe_instances_error: ClientError | None = None
        self.volume_missing = False
        self.paginator: FakeEc2Paginator | None = None

    def run_instances(self, **kwargs: JsonValue) -> AwsEc2Response:
        self.run_instances_kwargs = kwargs
        return {
            "Instances": [
                {
                    "InstanceId": "i-123",
                    "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-123"}}],
                }
            ]
        }

    def get_paginator(self, operation_name: str) -> FakeEc2Paginator:
        assert operation_name == "describe_instances"
        self.paginator = FakeEc2Paginator()
        return self.paginator

    def terminate_instances(self, **kwargs: JsonValue) -> None:
        self.terminate_instances_kwargs = kwargs

    def describe_instances(self, **kwargs: JsonValue) -> AwsEc2Response:
        self.describe_instances_kwargs = kwargs
        if self.describe_instances_error is not None:
            raise self.describe_instances_error
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-123",
                            "State": {"Name": self.instance_state},
                        }
                    ]
                }
            ]
        }

    def describe_regions(self, **kwargs: JsonValue) -> AwsEc2Response:
        self.describe_regions_kwargs = kwargs
        return {"Regions": [{"RegionName": "us-west-2"}]}

    def describe_volumes(self, **kwargs: JsonValue) -> AwsEc2Response:
        if self.volume_missing:
            raise ClientError(
                {"Error": {"Code": "InvalidVolume.NotFound", "Message": "missing"}},
                "DescribeVolumes",
            )
        return {"Volumes": [{"VolumeId": "vol-123", "State": "available"}]}


class FakeEc2Paginator:
    def __init__(self) -> None:
        self.paginate_kwargs: dict[str, JsonValue] | None = None

    def paginate(self, **kwargs: JsonValue) -> list[AwsEc2Response]:
        self.paginate_kwargs = kwargs
        return [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-123",
                                "Tags": [
                                    {
                                        "Key": AwsEc2TagKey.MachineId.value,
                                        "Value": "machine123",
                                    },
                                ],
                                "BlockDeviceMappings": [
                                    {"Ebs": {"VolumeId": "vol-123"}},
                                ],
                            },
                            {
                                "InstanceId": "i-missing-tag",
                                "Tags": [],
                            },
                        ]
                    }
                ]
            }
        ]

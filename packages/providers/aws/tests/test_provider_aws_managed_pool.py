from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import TypedDict

import pytest
from botocore.exceptions import ClientError
from compute.offers import ComputeOffer
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderPoolBootstrap,
    ProviderPoolRequest,
)
from provider_aws import (
    AwsAccountConnectionTarget,
    AwsConnectedAccountPooledProvider,
    AwsManagedPoolBinaries,
    AwsManagedPoolBootstrap,
    AwsManagedPoolClients,
    AwsManagedPoolPhase,
    AwsManagedPoolProvisioner,
    AwsManagedPoolProvisioningError,
    AwsManagedPoolResourceIds,
    AwsManagedPoolSpec,
    AwsProviderControlError,
    AwsProviderControlErrorCode,
)
from pydantic import SecretStr, TypeAdapter
from shared.compute_policy import ComputeCapacityMode, ComputePoolProviderState

_VPC_ID = "vpc-00000000000000001"
_SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
_SECURITY_GROUP_ID = "sg-00000000000000001"


class _Filter(TypedDict):
    Name: str
    Values: list[str]


_STRINGS = TypeAdapter(list[str])


class _Ec2:
    def __init__(self) -> None:
        self.launch_template = False
        self.launch_versions: dict[int, tuple[str, Mapping[str, object]]] = {}
        self.default_launch_version = 0
        self.launch_data: Mapping[str, object] = {}
        self.create_counts: dict[str, int] = {}
        self.volume_missing = False
        self.volume_results_empty = False
        self.volume_filters: list[_Filter] | None = None
        self.root_device_name = "/dev/xvda"

    def describe_images(self, *, ImageIds: list[str]) -> Mapping[str, object]:
        return {"Images": [{"ImageId": ImageIds[0], "RootDeviceName": self.root_device_name}]}

    def describe_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]:
        instances: list[Mapping[str, object]] = [
            {
                "InstanceId": instance_id,
                "State": {"Name": "terminated"},
                "BlockDeviceMappings": [
                    {"Ebs": {"VolumeId": f"vol-{instance_id.removeprefix('i-')}"}}
                ],
            }
            for instance_id in InstanceIds
        ]
        return {
            "Reservations": [
                {
                    "Instances": instances,
                }
            ]
        }

    def describe_volumes(
        self,
        *,
        VolumeIds: list[str] | None = None,
        Filters: list[_Filter] | None = None,
    ) -> Mapping[str, object]:
        if self.volume_results_empty:
            return {"Volumes": []}
        if self.volume_missing:
            if Filters is not None:
                self.volume_filters = Filters
                assert self._resource(Filters) == "volume"
                return {"Volumes": []}
            raise ClientError(
                {"Error": {"Code": "InvalidVolume.NotFound", "Message": "missing"}},
                "DescribeVolumes",
            )
        if Filters is not None:
            self.volume_filters = Filters
            assert self._resource(Filters) == "volume"
            return {"Volumes": [{"VolumeId": "vol-tagged0000000001"}]}
        assert VolumeIds is not None
        return {"Volumes": [{"VolumeId": item} for item in VolumeIds]}

    def _created(self, resource: str) -> None:
        self.create_counts[resource] = self.create_counts.get(resource, 0) + 1

    @staticmethod
    def _resource(Filters: list[_Filter]) -> str:
        for item in Filters:
            if item["Name"] == "tag:cloud-pool:resource":
                values = item["Values"]
                assert isinstance(values, list)
                return str(values[0])
        raise AssertionError("resource filter missing")

    def describe_launch_templates(self, **kwargs: object) -> Mapping[str, object]:
        templates: list[Mapping[str, object]] = (
            [
                {
                    "LaunchTemplateId": "lt-00000000000000001",
                    "LatestVersionNumber": max(self.launch_versions),
                }
            ]
            if self.launch_template
            else []
        )
        return {"LaunchTemplates": templates}

    def create_launch_template(self, **kwargs: object) -> Mapping[str, object]:
        self.launch_template = True
        description = str(kwargs["VersionDescription"])
        launch_data = kwargs["LaunchTemplateData"]
        assert isinstance(launch_data, Mapping)
        self.launch_data = launch_data
        self.launch_versions[1] = (description, launch_data)
        self.default_launch_version = 1
        self._created("launch-template")
        return {
            "LaunchTemplate": {
                "LaunchTemplateId": "lt-00000000000000001",
                "LatestVersionNumber": 1,
            }
        }

    def describe_launch_template_versions(self, **kwargs: object) -> Mapping[str, object]:
        versions = [int(value) for value in _STRINGS.validate_python(kwargs["Versions"])]
        records: list[Mapping[str, object]] = [
            {
                "VersionNumber": version,
                "VersionDescription": self.launch_versions[version][0],
            }
            for version in versions
        ]
        return {"LaunchTemplateVersions": records}

    def create_launch_template_version(self, **kwargs: object) -> Mapping[str, object]:
        version = max(self.launch_versions) + 1
        description = str(kwargs["VersionDescription"])
        launch_data = kwargs["LaunchTemplateData"]
        assert isinstance(launch_data, Mapping)
        self.launch_versions[version] = (description, launch_data)
        self.launch_data = launch_data
        return {
            "LaunchTemplateVersion": {
                "VersionNumber": version,
                "VersionDescription": description,
            }
        }

    def modify_launch_template(self, **kwargs: object) -> Mapping[str, object]:
        self.default_launch_version = int(str(kwargs["DefaultVersion"]))
        return {}

    def delete_launch_template(self, **kwargs: object) -> Mapping[str, object]:
        self.launch_template = False
        return {}


class _AutoScaling:
    def __init__(self) -> None:
        self.exists = False
        self.name = ""
        self.desired = 0
        self.minimum = 0
        self.maximum = 0
        self.tags: list[Mapping[str, object]] = []
        self.vpc_zone_identifier = ""
        self.launch_template: Mapping[str, object] = {}
        self.create_count = 0
        self.update_count = 0
        self.terminate_count = 0
        self.instances: list[Mapping[str, object]] = []

    def describe_auto_scaling_groups(self, **kwargs: object) -> Mapping[str, object]:
        groups: list[Mapping[str, object]] = (
            [
                {
                    "AutoScalingGroupName": self.name,
                    "DesiredCapacity": self.desired,
                    "MinSize": self.minimum,
                    "MaxSize": self.maximum,
                    "VPCZoneIdentifier": self.vpc_zone_identifier,
                    "LaunchTemplate": self.launch_template,
                    "Instances": self.instances,
                    "Tags": self.tags,
                }
            ]
            if self.exists
            else []
        )
        return {"AutoScalingGroups": groups}

    def create_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.exists = True
        self.name = str(kwargs["AutoScalingGroupName"])
        self.desired = int(str(kwargs["DesiredCapacity"]))
        self.minimum = int(str(kwargs["MinSize"]))
        self.maximum = int(str(kwargs["MaxSize"]))
        self.vpc_zone_identifier = str(kwargs["VPCZoneIdentifier"])
        tags = kwargs["Tags"]
        assert isinstance(tags, list)
        self.tags = tags
        launch_template = kwargs["LaunchTemplate"]
        assert isinstance(launch_template, Mapping)
        self.launch_template = launch_template
        self.create_count += 1
        return {}

    def update_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.desired = int(str(kwargs["DesiredCapacity"]))
        self.minimum = int(str(kwargs["MinSize"]))
        self.maximum = int(str(kwargs["MaxSize"]))
        self.vpc_zone_identifier = str(kwargs["VPCZoneIdentifier"])
        launch_template = kwargs["LaunchTemplate"]
        assert isinstance(launch_template, Mapping)
        self.launch_template = launch_template
        self.update_count += 1
        return {}

    def delete_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.exists = False
        return {}

    def set_desired_capacity(self, **kwargs: object) -> Mapping[str, object]:
        self.desired = int(str(kwargs["DesiredCapacity"]))
        return {}

    def terminate_instance_in_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.terminate_count += 1
        return {}


class _ClientProvider:
    def __init__(self, clients: AwsManagedPoolClients) -> None:
        self.clients = clients

    def assume(self, target: AwsAccountConnectionTarget) -> AwsManagedPoolClients:
        target.validated_scope()
        return self.clients


def _spec() -> AwsManagedPoolSpec:
    return AwsManagedPoolSpec(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        pool_name="acceptance",
        region="us-east-1",
        instance_type="i4i.xlarge",
        ami_id="ami-0123456789abcdef0",
        desired_nodes=1,
        max_nodes=2,
        root_volume_gib=50,
        node_instance_profile_arn=("arn:aws:iam::123456789012:instance-profile/compute-node"),
        vpc_id=_VPC_ID,
        subnet_ids=_SUBNET_IDS,
        security_group_id=_SECURITY_GROUP_ID,
        bootstrap=AwsManagedPoolBootstrap(
            control_plane_url="https://compute.example.com",
            enrollment_request_id="12345678-1234-4123-8123-123456789abc",
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
        ),
    )


def _connection_target(
    *,
    vpc_id: str | None = _VPC_ID,
    subnet_ids: tuple[str, ...] = _SUBNET_IDS,
    security_group_id: str | None = _SECURITY_GROUP_ID,
) -> AwsAccountConnectionTarget:
    return AwsAccountConnectionTarget(
        account_id="123456789012",
        region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/lazycloud-control",
        external_id=SecretStr("x" * 48),
        node_role_arn="arn:aws:iam::123456789012:role/lazycloud-node",
        node_instance_profile_arn=("arn:aws:iam::123456789012:instance-profile/lazycloud-node"),
        vpc_id=vpc_id,
        subnet_ids=subnet_ids,
        security_group_id=security_group_id,
    )


def test_managed_pool_ensure_is_idempotent_and_launches_into_the_stack_network() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    checkpoints: list[AwsManagedPoolResourceIds] = []

    created = provisioner.ensure(_spec(), progress=checkpoints.append)
    repeated = provisioner.ensure(
        _spec(),
        created.resource_ids,
        progress=checkpoints.append,
    )

    assert created.phase is AwsManagedPoolPhase.Provisioning
    assert created.resource_ids.complete is True
    assert repeated.resource_ids == created.resource_ids
    assert checkpoints[-1].complete is True
    assert autoscaling.create_count == 1
    assert autoscaling.update_count == 0
    assert autoscaling.vpc_zone_identifier == ",".join(_SUBNET_IDS)
    assert set(ec2.create_counts) == {"launch-template"}
    assert ec2.launch_data["SecurityGroupIds"] == [_SECURITY_GROUP_ID]
    metadata = ec2.launch_data["MetadataOptions"]
    assert isinstance(metadata, Mapping)
    assert metadata["HttpTokens"] == "required"
    encoded_user_data = ec2.launch_data["UserData"]
    assert isinstance(encoded_user_data, str)
    user_data = base64.b64decode(encoded_user_data).decode()
    assert f"AGENT_SHA256={'a' * 64}" in user_data
    assert f"WORKER_IMAGE_DIGEST=registry.example.com/worker@sha256:{'b' * 64}" in user_data
    assert "ENROLLMENT_REQUEST_ID=12345678-1234-4123-8123-123456789abc" in user_data
    assert "X-aws-ec2-metadata-token" in user_data
    assert "--provider-instance-identity imds-v2" in user_data
    assert '--provider-enrollment-request "$ENROLLMENT_REQUEST_ID"' in user_data
    assert "--provider aws" in user_data
    assert "trap bootstrap_failed ERR" in user_data
    assert "systemctl poweroff" not in user_data
    assert "shutdown -h" not in user_data
    assert "--join-token" not in user_data
    assert "--cloud-" not in user_data
    assert "tailscale-auth" not in user_data


def test_managed_pool_storage_destruction_requires_exact_volume_absence() -> None:
    ec2 = _Ec2()
    provisioner = AwsManagedPoolProvisioner(
        AwsManagedPoolClients(ec2=ec2, autoscaling=_AutoScaling())
    )
    instance_id = "i-00000000000000001"
    volume_id = "vol-00000000000000001"

    assert provisioner.storage_volume_ids((instance_id,)) == {instance_id: (volume_id,)}
    assert not provisioner.machine_storage_destroyed(_spec(), instance_id, (volume_id,))
    assert not provisioner.machine_storage_destroyed(_spec(), instance_id, ())

    ec2.volume_results_empty = True
    assert provisioner.machine_storage_destroyed(
        _spec(),
        instance_id,
        (volume_id, "vol-00000000000000002"),
    )
    ec2.volume_results_empty = False
    ec2.volume_missing = True
    assert provisioner.machine_storage_destroyed(_spec(), instance_id, (volume_id,))
    assert provisioner.machine_storage_destroyed(_spec(), instance_id, ())
    assert ec2.volume_filters is not None
    filters = {item["Name"]: item["Values"] for item in ec2.volume_filters}
    assert filters == {
        "tag:cloud-pool:managed-by": ["control-plane"],
        "tag:cloud-pool:key": [_spec().resource_key],
        "tag:cloud-pool:resource": ["volume"],
    }


def _pool_request(provider_ref: str) -> ProviderPoolRequest:
    return ProviderPoolRequest(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        pool_id="22345678-1234-4123-8123-123456789abc",
        pool_name="managed-capacity",
        provider_ref=provider_ref,
        provider_connection_id="12345678-1234-4123-8123-123456789abc",
        generation=1,
        offer=ComputeOffer(
            id="us-east-1:i4i.xlarge",
            provider=provider_ref,
            cloud="aws",
            instance_type="i4i.xlarge",
            region="us-east-1",
            cpu_millicores=4_000,
            memory_mb=32_768,
            storage_mb=204_800,
            hourly_cost_micros=340_000,
            available=100,
            capacity_mode=ComputeCapacityMode.Pooled,
            capability_key="aws:us-east-1:i4i.xlarge:amd64:runc",
            supports_scale_to_zero=True,
        ),
        desired_machines=0,
        max_machines=3,
        bootstrap=ProviderPoolBootstrap(
            control_plane_url="https://compute.example.com",
            enrollment_request_id="22345678-1234-4123-8123-123456789abc",
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
        ),
        provider_state=ComputePoolProviderState(),
    )


def test_pooled_provider_scales_and_reports_machine_infrastructure_health() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provider = AwsConnectedAccountPooledProvider(
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        connection=_connection_target(),
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        instance_hourly_micros={"i4i.xlarge": 340_000},
        allowed_instance_types=frozenset({"i4i.xlarge"}),
        client_provider=_ClientProvider(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling)),
    )
    request = _pool_request(provider.provider_ref)

    created = provider.set_pool_capacity(request, desired_machines=1, max_machines=3)
    updated = provider.set_pool_capacity(
        request.model_copy(update={"provider_state": created.provider_state}),
        desired_machines=2,
        max_machines=3,
    )

    assert created.desired_machines == 1
    assert updated.desired_machines == 2
    assert autoscaling.create_count == 1
    assert autoscaling.update_count == 1
    assert autoscaling.vpc_zone_identifier == ",".join(_SUBNET_IDS)
    assert all(count == 1 for count in ec2.create_counts.values())

    first_instance = "i-00000000000000001"
    second_instance = "i-00000000000000002"
    autoscaling.instances = [
        {
            "InstanceId": first_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1a",
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1b",
        },
    ]
    observed_request = request.model_copy(
        update={
            "desired_machines": 2,
            "provider_state": updated.provider_state,
        }
    )
    ready = provider.describe_pool(observed_request)
    assert ready.phase is ProviderCapacityPhase.Ready
    assert {instance.status for instance in ready.instances} == {ProviderMachineStatus.Active}

    autoscaling.instances = [
        {
            "InstanceId": first_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1a",
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Unhealthy",
            "AvailabilityZone": "us-east-1b",
        },
    ]
    degraded = provider.describe_pool(observed_request)
    statuses = {instance.provider_instance_id: instance.status for instance in degraded.instances}
    assert degraded.phase is not ProviderCapacityPhase.Ready
    assert statuses[first_instance] is ProviderMachineStatus.Active
    assert statuses[second_instance] is ProviderMachineStatus.Pending

    autoscaling.instances = [
        {
            "InstanceId": first_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1a",
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "Pending",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1b",
        },
    ]
    launching = provider.describe_pool(observed_request)
    launching_statuses = {
        instance.provider_instance_id: instance.status for instance in launching.instances
    }
    assert launching.phase is not ProviderCapacityPhase.Ready
    assert launching_statuses[second_instance] is ProviderMachineStatus.Pending


def test_pooled_provider_requires_the_stack_provisioned_network() -> None:
    provider = AwsConnectedAccountPooledProvider(
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        connection=_connection_target(vpc_id=None, subnet_ids=(), security_group_id=None),
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        instance_hourly_micros={"i4i.xlarge": 340_000},
        allowed_instance_types=frozenset(),
        client_provider=_ClientProvider(
            AwsManagedPoolClients(ec2=_Ec2(), autoscaling=_AutoScaling())
        ),
    )

    with pytest.raises(ValueError, match="no stack-provisioned network"):
        provider.set_pool_capacity(
            _pool_request(provider.provider_ref),
            desired_machines=1,
            max_machines=3,
        )


def test_pooled_provider_does_not_offer_unpriced_instance_types() -> None:
    provider = AwsConnectedAccountPooledProvider(
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        connection=_connection_target(),
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
                cpu_ami_id="ami-0123456789abcdef0",
                gpu_ami_id="ami-1234567890abcdef0",
            )
        },
        instance_hourly_micros={"i4i.xlarge": 340_000},
        allowed_instance_types=frozenset(),
        client_provider=_ClientProvider(
            AwsManagedPoolClients(ec2=_Ec2(), autoscaling=_AutoScaling())
        ),
    )

    offers = list(provider.list_offers())

    assert [offer.instance_type for offer in offers] == ["i4i.xlarge"]
    assert offers[0].hourly_cost_micros == 340_000


def test_managed_pool_artifact_change_versions_template_and_updates_group() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    initial_spec = _spec()
    created = provisioner.ensure(initial_spec)
    upgraded_spec = initial_spec.model_copy(
        update={
            "bootstrap": initial_spec.bootstrap.model_copy(
                update={
                    "agent_version": "0.2.0",
                    "agent_sha256": "c" * 64,
                    "worker_image_digest": (f"registry.example.com/worker@sha256:{'d' * 64}"),
                }
            )
        }
    )

    upgraded = provisioner.ensure(upgraded_spec, created.resource_ids)
    repeated = provisioner.ensure(upgraded_spec, upgraded.resource_ids)

    assert created.resource_ids.launch_template_version == 1
    assert upgraded.resource_ids.launch_template_version == 2
    assert repeated.resource_ids.launch_template_version == 2
    assert sorted(ec2.launch_versions) == [1, 2]
    assert ec2.default_launch_version == 2
    assert autoscaling.launch_template == {
        "LaunchTemplateId": "lt-00000000000000001",
        "Version": "2",
    }
    assert autoscaling.update_count == 1
    encoded_user_data = ec2.launch_data["UserData"]
    assert isinstance(encoded_user_data, str)
    user_data = base64.b64decode(encoded_user_data).decode()
    assert f"AGENT_SHA256={'c' * 64}" in user_data
    assert f"WORKER_IMAGE_DIGEST=registry.example.com/worker@sha256:{'d' * 64}" in user_data


def test_managed_pool_delete_converges_after_asg_instance_cleanup() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    created = provisioner.ensure(_spec())

    requested = provisioner.delete(_spec(), created.resource_ids)
    deleted = provisioner.delete(_spec(), requested.resource_ids)

    assert requested.phase is AwsManagedPoolPhase.Deleting
    assert deleted.phase is AwsManagedPoolPhase.Deleted
    assert deleted.resource_ids == AwsManagedPoolResourceIds()


def test_managed_pool_partial_failure_returns_last_durable_checkpoint() -> None:
    class _FailingAutoScaling(_AutoScaling):
        def create_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.UpstreamUnavailable,
                operation="create Auto Scaling Group",
                detail="capacity API unavailable",
            )

    checkpoints: list[AwsManagedPoolResourceIds] = []
    provisioner = AwsManagedPoolProvisioner(
        AwsManagedPoolClients(ec2=_Ec2(), autoscaling=_FailingAutoScaling())
    )

    with pytest.raises(AwsManagedPoolProvisioningError) as caught:
        provisioner.ensure(_spec(), progress=checkpoints.append)

    assert caught.value.resource_ids.launch_template_id == "lt-00000000000000001"
    assert caught.value.resource_ids.autoscaling_group_name is None
    assert checkpoints[-1] == caught.value.resource_ids


def test_managed_pool_rejects_same_named_group_without_ownership_tags() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    provisioner.ensure(_spec())
    autoscaling.tags = []

    with pytest.raises(AwsProviderControlError) as caught:
        provisioner.scale(_spec(), desired_nodes=1, max_nodes=2)

    assert caught.value.code is AwsProviderControlErrorCode.InvalidResponse
    assert "not owned" in caught.value.detail


def test_managed_pool_maps_malformed_aws_inventory_to_typed_error() -> None:
    class _MalformedEc2(_Ec2):
        def describe_launch_templates(self, **kwargs: object) -> Mapping[str, object]:
            del kwargs
            malformed_templates: list[Mapping[str, object]] = [{}]
            return {"LaunchTemplates": malformed_templates}

    provisioner = AwsManagedPoolProvisioner(
        AwsManagedPoolClients(ec2=_MalformedEc2(), autoscaling=_AutoScaling())
    )

    with pytest.raises(AwsManagedPoolProvisioningError) as caught:
        provisioner.ensure(_spec())

    assert caught.value.code is AwsProviderControlErrorCode.InvalidResponse
    assert caught.value.resource_ids == AwsManagedPoolResourceIds()

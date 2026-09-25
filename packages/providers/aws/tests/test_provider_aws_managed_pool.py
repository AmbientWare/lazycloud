from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from compute.offers import ComputeOffer, OfferRequest, choose_offer
from compute.provider_state import ProviderUnitStateService
from compute.providers import (
    ProviderCapacityPhase,
    ProviderMachineStatus,
    ProviderUnitBootstrap,
    ProviderUnitRequest,
)
from database.context import ServiceContext
from database.repositories.compute import ComputeUnitRepository
from identity.platform import PlatformNamespaceService
from provider_aws.account_connection import AwsAccountConnectionTarget
from provider_aws.managed_pool import (
    AWS_MANAGED_POOL_TAG,
    AWS_MANAGED_POOL_TAG_VALUE,
    AwsManagedPoolBinaries,
    AwsManagedPoolBootstrap,
    AwsManagedPoolClients,
    AwsManagedPoolPhase,
    AwsManagedPoolProvisioner,
    AwsManagedPoolProvisioningError,
    AwsManagedPoolResourceIds,
    AwsManagedPoolSpec,
    _ScalingActivity,
)
from provider_aws.network_egress import NetworkFilter
from provider_aws.pooled_provider import AwsPooledCapacityProvider
from provider_aws.provider_control import AwsProviderControlError, AwsProviderControlErrorCode
from provider_aws.retained_pool import AwsRetainedPool, RetainedPoolState, RetainedSlot, SlotPhase
from provider_aws.spot_prices import AwsSpotQuoteCache
from provider_aws.supplier_prices import AwsRegionalPrices
from pydantic import SecretStr, TypeAdapter, ValidationError
from shared.aws_connections import AwsAccountNetwork
from shared.capacity import CapacityFailureCode, CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitProviderState,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    UnitName,
)
from shared.placement import Placement
from shared.supplier_costs import SupplierCostTerms
from shared.timestamps import utc_now

_VPC_ID = "vpc-00000000000000001"
_SUBNET_IDS = ("subnet-00000000000000001", "subnet-00000000000000002")
_SECURITY_GROUP_ID = "sg-00000000000000001"
_NETWORK = AwsAccountNetwork(
    vpc_id=_VPC_ID,
    subnet_ids=_SUBNET_IDS,
    security_group_id=_SECURITY_GROUP_ID,
)


class _Filter(TypedDict):
    Name: str
    Values: list[str]


_STRINGS = TypeAdapter(list[str])


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Max spot instance count exceeded.", CapacityFailureCode.ProviderQuotaExceeded),
        (
            "You have requested more vCPU capacity than your current vCPU limit of 128 allows.",
            CapacityFailureCode.ProviderQuotaExceeded,
        ),
        (
            "We currently do not have sufficient g4dn.xlarge capacity in the Availability Zone.",
            CapacityFailureCode.CapacityUnavailable,
        ),
        ("The requested configuration is not supported.", CapacityFailureCode.ProviderLaunchFailed),
    ],
)
def test_scaling_failure_distinguishes_quota_from_capacity(
    message: str,
    expected: CapacityFailureCode,
) -> None:
    activity = _ScalingActivity.model_validate(
        {
            "StartTime": datetime(2026, 9, 15, tzinfo=UTC),
            "StatusCode": "Failed",
            "StatusMessage": message,
        }
    )
    assert activity.failure_code is expected


class _Ec2:
    def run_instances(self, **kwargs: object) -> Mapping[str, object]:
        raise AssertionError("ASG lifecycle must not directly launch instances")

    def start_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]:
        raise AssertionError("ASG lifecycle must not directly start instances")

    def stop_instances(
        self, *, InstanceIds: list[str], Hibernate: bool = False, Force: bool = False
    ) -> Mapping[str, object]:
        raise AssertionError("ASG lifecycle must not directly stop instances")

    def cancel_spot_instance_requests(
        self, *, SpotInstanceRequestIds: list[str]
    ) -> Mapping[str, object]:
        raise AssertionError("ASG lifecycle must not cancel persistent requests")

    def describe_spot_instance_requests(
        self, *, SpotInstanceRequestIds: list[str]
    ) -> Mapping[str, object]:
        raise AssertionError("ASG lifecycle must not describe persistent requests")

    def terminate_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]:
        raise AssertionError("this scenario must not terminate retained instances")

    def describe_route_tables(
        self, *, Filters: list[NetworkFilter], NextToken: str = ""
    ) -> Mapping[str, object]:
        raise AssertionError("capacity lifecycle must not inspect network billing routes")

    def describe_vpc_endpoints(self, *, VpcEndpointIds: list[str]) -> Mapping[str, object]:
        raise AssertionError("capacity lifecycle must not inspect network billing routes")

    def describe_managed_prefix_lists(self, *, PrefixListIds: list[str]) -> Mapping[str, object]:
        raise AssertionError("capacity lifecycle must not inspect network billing routes")

    def get_managed_prefix_list_entries(
        self, *, PrefixListId: str, NextToken: str = ""
    ) -> Mapping[str, object]:
        raise AssertionError("capacity lifecycle must not inspect network billing routes")

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

    def describe_subnets(self, *, SubnetIds: list[str]) -> Mapping[str, object]:
        return {
            "Subnets": [
                {
                    "SubnetId": subnet_id,
                    "VpcId": _VPC_ID,
                    "AvailabilityZoneId": f"use1-az{index + 1}",
                }
                for index, subnet_id in enumerate(_SUBNET_IDS)
                if subnet_id in SubnetIds
            ]
        }

    def describe_spot_price_history(self, **kwargs: object) -> Mapping[str, object]:
        return {"SpotPriceHistory": []}

    def describe_instances(
        self,
        *,
        InstanceIds: list[str] | None = None,
        Filters: list[_Filter] | None = None,
        NextToken: str = "",
    ) -> Mapping[str, object]:
        assert InstanceIds is not None
        instances: list[Mapping[str, object]] = [
            {
                "InstanceId": instance_id,
                "Placement": {"AvailabilityZoneId": "use1-az1"},
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
                "LaunchTemplateData": self.launch_versions[version][1],
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
                "LaunchTemplateData": self.launch_versions[version][1],
            }
        }

    def modify_launch_template(self, **kwargs: object) -> Mapping[str, object]:
        self.default_launch_version = int(str(kwargs["DefaultVersion"]))
        return {}

    def delete_launch_template(self, **kwargs: object) -> Mapping[str, object]:
        self.launch_template = False
        return {}


class _AutoScaling:
    def describe_scaling_activities(self, **kwargs: object) -> Mapping[str, object]:
        return {"Activities": []}

    def __init__(self) -> None:
        self.exists = False
        self.name = ""
        self.desired = 0
        self.minimum = 0
        self.protect_new_instances = False
        self.maximum = 0
        self.tags: list[Mapping[str, object]] = []
        self.vpc_zone_identifier = ""
        self.launch_template: Mapping[str, object] = {}
        self.create_count = 0
        self.update_count = 0
        self.terminate_count = 0
        self.instances: list[Mapping[str, object]] = []
        self.suspended_processes: set[str] = set()

    def describe_auto_scaling_groups(self, **kwargs: object) -> Mapping[str, object]:
        groups: list[Mapping[str, object]] = (
            [
                {
                    "AutoScalingGroupName": self.name,
                    "DesiredCapacity": self.desired,
                    "MinSize": self.minimum,
                    "NewInstancesProtectedFromScaleIn": self.protect_new_instances,
                    "MaxSize": self.maximum,
                    "VPCZoneIdentifier": self.vpc_zone_identifier,
                    "LaunchTemplate": self.launch_template,
                    "Instances": self.instances,
                    "Tags": self.tags,
                    "SuspendedProcesses": [
                        {"ProcessName": name} for name in sorted(self.suspended_processes)
                    ],
                }
            ]
            if self.exists
            else []
        )
        return {"AutoScalingGroups": groups}

    def suspend_processes(
        self, *, AutoScalingGroupName: str, ScalingProcesses: list[str]
    ) -> Mapping[str, object]:
        assert AutoScalingGroupName == self.name
        self.suspended_processes.update(ScalingProcesses)
        return {}

    def resume_processes(
        self, *, AutoScalingGroupName: str, ScalingProcesses: list[str]
    ) -> Mapping[str, object]:
        assert AutoScalingGroupName == self.name
        self.suspended_processes.difference_update(ScalingProcesses)
        return {}

    def create_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.exists = True
        self.name = str(kwargs["AutoScalingGroupName"])
        self.desired = int(str(kwargs["DesiredCapacity"]))
        self.minimum = int(str(kwargs["MinSize"]))
        self.protect_new_instances = bool(kwargs["NewInstancesProtectedFromScaleIn"])
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
        self.protect_new_instances = bool(kwargs["NewInstancesProtectedFromScaleIn"])
        self.desired = int(str(kwargs["DesiredCapacity"]))
        self.minimum = int(str(kwargs["MinSize"]))
        self.maximum = int(str(kwargs["MaxSize"]))
        self.vpc_zone_identifier = str(kwargs["VPCZoneIdentifier"])
        launch_template = kwargs["LaunchTemplate"]
        assert isinstance(launch_template, Mapping)
        self.launch_template = launch_template
        self.update_count += 1
        return {}

    def set_instance_protection(
        self, *, AutoScalingGroupName: str, InstanceIds: list[str], ProtectedFromScaleIn: bool
    ) -> Mapping[str, object]:
        self.instances = [
            {**instance, "ProtectedFromScaleIn": ProtectedFromScaleIn}
            if instance["InstanceId"] in InstanceIds
            else instance
            for instance in self.instances
        ]
        return {}

    def delete_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
        self.exists = False
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


def _spec(*, desired_nodes: int = 1, max_nodes: int = 2) -> AwsManagedPoolSpec:
    return AwsManagedPoolSpec(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        unit_name=UnitName("acceptance"),
        region="us-east-1",
        instance_type="m7i.2xlarge",
        ami_id="ami-0123456789abcdef0",
        desired_nodes=desired_nodes,
        max_nodes=max_nodes,
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
                "lazycloud-agent-linux-amd64.tar.gz"
            ),
        ),
    )


class _RetainedEc2(_Ec2):
    retry_attempts: int = 0
    spot_request_state: str = "active"
    spot_instance_id: str = ""

    def __init__(self) -> None:
        super().__init__()
        self.instances: dict[str, dict[str, object]] = {}
        self.terminated: list[str] = []

    def run_instances(self, **kwargs: object) -> Mapping[str, object]:
        raise ClientError(
            {
                "Error": {
                    "Code": "InsufficientInstanceCapacity",
                    "Message": "capacity unavailable",
                },
                "ResponseMetadata": {
                    "RetryAttempts": self.retry_attempts,
                    "RequestId": "test-request",
                    "HostId": "",
                    "HTTPStatusCode": 500,
                    "HTTPHeaders": {},
                },
            },
            "RunInstances",
        )

    def describe_instances(
        self,
        *,
        InstanceIds: list[str] | None = None,
        Filters: list[_Filter] | None = None,
        NextToken: str = "",
    ) -> Mapping[str, object]:
        if InstanceIds:
            if not set(InstanceIds) <= self.instances.keys():
                raise ClientError(
                    {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "missing"}},
                    "DescribeInstances",
                )
            found = [self.instances[instance_id] for instance_id in InstanceIds]
        else:
            assert Filters is not None
            [query] = Filters
            field = {
                "client-token": "ClientToken",
                "spot-instance-request-id": "SpotInstanceRequestId",
            }
            found = [
                instance
                for instance in self.instances.values()
                if instance.get(field[query["Name"]]) in query["Values"]
            ]
        return {"Reservations": [{"Instances": found}] if found else []}

    def terminate_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]:
        assert self.spot_request_state == "cancelled"
        for instance_id in InstanceIds:
            self.instances[instance_id] |= {
                "State": {"Name": "terminated"},
                "BlockDeviceMappings": [],
            }
        self.terminated += InstanceIds
        return {}

    def describe_spot_instance_requests(
        self, *, SpotInstanceRequestIds: list[str]
    ) -> Mapping[str, object]:
        return {
            "SpotInstanceRequests": [
                {
                    "SpotInstanceRequestId": SpotInstanceRequestIds[0],
                    "State": self.spot_request_state,
                    "InstanceId": self.spot_instance_id,
                }
            ]
        }

    def cancel_spot_instance_requests(
        self, *, SpotInstanceRequestIds: list[str]
    ) -> Mapping[str, object]:
        self.spot_request_state = "cancelled"
        return {}


def _save_slot(pool: AwsRetainedPool, request: ProviderUnitRequest, slot: RetainedSlot) -> None:
    state = pool.checkpoints.load(request)
    pool.checkpoints.save(
        request,
        expected=state,
        state=state.model_copy(
            update={
                "revision": state.revision + 1,
                "attributes": RetainedPoolState(
                    namespace_id=pool.spec.workspace_id, slots=(slot,)
                ).model_dump(mode="json"),
            }
        ),
    )


@pytest.fixture
def retained_pool(service_context: ServiceContext) -> tuple[AwsRetainedPool, _RetainedEc2]:
    workspace = PlatformNamespaceService(service_context.database).initialize()
    request = _pool_request("aws:retained-test").model_copy(
        update={"workspace_id": workspace.id, "provider_connection_id": None, "stopped_machines": 1}
    )
    with service_context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=request.unit_id,
                name=request.unit_name,
                workspace_id=workspace.id,
                capacity_owner_id=request.unit_id,
                capacity_owner_kind=CapacityOwnerKind.PooledProvider,
                capacity_owner_source=CapacityOwnerSource.Provider,
                placement=Placement.platform(),
                platform_fleet=True,
                provider="aws",
                provider_ref=request.provider_ref,
                region=request.offer.region,
                offer_id=request.offer.id,
                capability_key=request.offer.capability_key,
                capacity_mode=ComputeCapacityMode.Pooled,
                visibility=ComputeUnitVisibility.Internal,
                stopped_machines=1,
                max_machines=3,
            )
        )
    ec2 = _RetainedEc2()
    return (
        AwsRetainedPool(
            request,
            _spec().model_copy(update={"workspace_id": workspace.id}),
            AwsManagedPoolClients(ec2=ec2, autoscaling=_AutoScaling()),
            ProviderUnitStateService(service_context.database),
        ),
        ec2,
    )


@pytest.mark.parametrize("sdk_retries", [0, 1])
def test_rejected_launch_releases_only_proven_unused_capacity(
    retained_pool: tuple[AwsRetainedPool, _RetainedEc2],
    sdk_retries: int,
    service_context: ServiceContext,
) -> None:
    pool, ec2 = retained_pool
    ec2.retry_attempts = sdk_retries
    if sdk_retries:
        with pytest.raises(AwsProviderControlError):
            pool.ensure()
    else:
        snapshot = pool.ensure()
        assert snapshot.last_capacity_failure_at is not None
        assert snapshot.last_capacity_failure_code is CapacityFailureCode.CapacityUnavailable
    state = pool.checkpoints.load(pool.request)
    assert state.committed_machines == int(bool(sdk_retries))
    with service_context.database.session() as session:
        units = ComputeUnitRepository(session)
        unit = units.get(pool.request.unit_id)
        assert unit is not None
        units.upsert(unit.model_copy(update={"desired_machines": 0, "stopped_machines": 0}))
        assert units.platform_capacity_usage(gpu=False) == int(bool(sdk_retries))
    [slot] = RetainedPoolState.model_validate(state.attributes).slots
    if sdk_retries:
        assert slot.launch_started_at is not None
        ambiguous = slot.model_copy(update={"launch_started_at": utc_now() - timedelta(minutes=20)})
        pool.checkpoints.save(
            pool.request,
            expected=state,
            state=state.model_copy(
                update={
                    "revision": state.revision + 1,
                    "attributes": pool.state.model_copy(update={"slots": (ambiguous,)}).model_dump(
                        mode="json"
                    ),
                }
            ),
        )
    restarted = AwsRetainedPool(pool.request, pool.spec, pool.clients, pool.checkpoints)
    if sdk_retries:
        with pytest.raises(RuntimeError, match="no instance evidence"):
            restarted.delete()
        assert RetainedPoolState.model_validate(
            pool.checkpoints.load(pool.request).attributes
        ).slots
        with service_context.database.session() as session:
            assert ComputeUnitRepository(session).platform_capacity_usage(gpu=False) == 1
    else:
        assert slot.launch_started_at is None
        assert restarted.delete().phase is ProviderCapacityPhase.Deleted
        assert not RetainedPoolState.model_validate(
            pool.checkpoints.load(pool.request).attributes
        ).slots


def test_missing_stopped_instance_releases_slot_after_storage_and_request_cleanup(
    retained_pool: tuple[AwsRetainedPool, _RetainedEc2],
) -> None:
    pool, ec2 = retained_pool
    now = utc_now()
    slot = RetainedSlot(
        token=uuid4().hex,
        created_at=now - timedelta(hours=2),
        launch_started_at=now - timedelta(hours=2),
        launch_template_id="lt-retained",
        launch_template_version=1,
        host_revision="host",
        subnet_id=_SUBNET_IDS[0],
        serving=False,
        phase=SlotPhase.Stopped,
        instance_id="i-00000000000000001",
        spot_request_id="sir-retained",
        storage_volume_ids=("vol-00000000000000001",),
    )
    _save_slot(pool, pool.request, slot)
    request = pool.request.model_copy(update={"purchases_enabled": False})
    held = AwsRetainedPool(request, pool.spec, pool.clients, pool.checkpoints)
    held.ensure()
    assert held.state.slots[0].phase is SlotPhase.Stopped
    ec2.volume_missing = True
    held.ensure()
    assert held.state.slots[0].phase is SlotPhase.Retiring
    assert ec2.spot_request_state == "cancelled"
    held.ensure()
    assert not held.state.slots
    assert not RetainedPoolState.model_validate(pool.checkpoints.load(request).attributes).slots


def test_retiring_slot_cancels_its_persistent_request_and_terminates_its_relaunch(
    retained_pool: tuple[AwsRetainedPool, _RetainedEc2],
) -> None:
    pool, ec2 = retained_pool
    now = utc_now()
    slot = RetainedSlot(
        token=uuid4().hex,
        created_at=now - timedelta(hours=2),
        launch_started_at=now - timedelta(hours=2),
        launch_template_id="lt-retained",
        launch_template_version=1,
        host_revision="host",
        subnet_id=_SUBNET_IDS[0],
        serving=True,
        phase=SlotPhase.Active,
        instance_id="i-00000000000000001",
        spot_request_id="sir-retained",
        storage_volume_ids=("vol-00000000000000001",),
    )
    ec2.instances = {
        "i-00000000000000001": {
            "InstanceId": "i-00000000000000001",
            "ClientToken": slot.token,
            "State": {"Name": "terminated"},
            "SpotInstanceRequestId": "sir-retained",
            "Tags": [
                {"Key": AWS_MANAGED_POOL_TAG, "Value": AWS_MANAGED_POOL_TAG_VALUE},
                {"Key": "cloud-pool:key", "Value": pool.spec.resource_key},
                {"Key": "cloud-pool:workspace", "Value": pool.spec.workspace_id},
            ],
        },
        "i-00000000000000002": {
            "InstanceId": "i-00000000000000002",
            "State": {"Name": "running"},
            "SpotInstanceRequestId": "sir-retained",
            "BlockDeviceMappings": [
                {"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": "vol-00000000000000002"}}
            ],
        },
    }
    ec2.spot_instance_id = "i-00000000000000002"
    _save_slot(pool, pool.request, slot)
    request = pool.request.model_copy(update={"purchases_enabled": False})
    held = AwsRetainedPool(request, pool.spec, pool.clients, pool.checkpoints)
    held.ensure()
    assert held.state.slots[0].phase is SlotPhase.Retiring
    assert ec2.spot_request_state == "cancelled"
    assert not ec2.terminated
    held.ensure()
    held.ensure()
    assert ec2.terminated == ["i-00000000000000002"]
    assert held.state.slots[0].storage_volume_ids == (
        "vol-00000000000000001",
        "vol-00000000000000002",
    )
    ec2.volume_missing = True
    held.ensure()
    assert not held.state.slots
    assert held.checkpoints.load(request).committed_machines == 0


def test_managed_pool_rejects_desired_capacity_above_its_allocation() -> None:
    spec = _spec(desired_nodes=101, max_nodes=500)
    assert spec.desired_nodes == 101
    assert spec.max_nodes == 500
    with pytest.raises(ValidationError, match="desired_nodes cannot exceed max_nodes"):
        _spec(desired_nodes=501, max_nodes=500)


def test_disabled_pool_does_not_create_capacity_or_launch_templates() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))

    snapshot = provisioner.ensure(_spec().model_copy(update={"purchases_enabled": False}))

    assert snapshot.phase is AwsManagedPoolPhase.Deleted
    assert snapshot.desired_nodes == 0
    assert not autoscaling.exists
    assert not ec2.launch_template


def test_disabled_pool_preserves_instances_and_other_suspended_processes() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    spec = _spec(desired_nodes=2)
    provisioner.ensure(spec)
    autoscaling.instances = [
        {
            "InstanceId": f"i-{index:017x}",
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "ProtectedFromScaleIn": True,
            "LaunchTemplate": autoscaling.launch_template,
        }
        for index in (1, 2)
    ]
    autoscaling.suspended_processes.add("AZRebalance")
    disabled = spec.model_copy(update={"purchases_enabled": False, "root_volume_gib": 100})

    observed = provisioner.ensure(disabled)
    assert observed.desired_nodes == 2
    assert len(observed.instances) == 2
    assert autoscaling.suspended_processes == {"Launch", "AZRebalance"}
    assert len(ec2.launch_versions) == 1

    provisioner.scale(disabled, desired_nodes=1, max_nodes=2)
    assert autoscaling.desired == 1
    assert autoscaling.suspended_processes == {"Launch", "AZRebalance"}
    assert len(ec2.launch_versions) == 1
    with pytest.raises(ValueError, match="purchases are disabled"):
        provisioner.scale(disabled, desired_nodes=2, max_nodes=2)
    assert autoscaling.desired == 1

    assert provisioner.release_instance(disabled, "i-00000000000000001")
    assert autoscaling.terminate_count == 1
    assert autoscaling.suspended_processes == {"Launch", "AZRebalance"}

    enabled = disabled.model_copy(update={"purchases_enabled": True, "desired_nodes": 1})
    provisioner.ensure(enabled)
    assert autoscaling.desired == 1
    assert autoscaling.launch_template["Version"] == "2"
    assert autoscaling.suspended_processes == {"AZRebalance"}

    provisioner.delete(disabled)
    provisioner.delete(disabled)
    assert not autoscaling.exists
    assert not ec2.launch_template


def test_managed_pool_rejects_unavailable_zone_before_creating_resources() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    spec = _spec().model_copy(update={"availability_zone": "use1-az3"})

    with pytest.raises(AwsManagedPoolProvisioningError, match="no subnet in availability zone"):
        provisioner.ensure(spec)

    assert not ec2.launch_template
    assert not autoscaling.exists


def _connection_target(
    *,
    network: AwsAccountNetwork | None = None,
) -> AwsAccountConnectionTarget:
    return AwsAccountConnectionTarget(
        account_id="123456789012",
        region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/lazycloud-control",
        external_id=SecretStr("x" * 48),
        node_role_arn="arn:aws:iam::123456789012:role/lazycloud-node",
        node_instance_profile_arn=("arn:aws:iam::123456789012:instance-profile/lazycloud-node"),
        network=network if network is not None else _NETWORK,
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
    assert "WORKER_IMAGE_DIGEST" not in user_data
    assert "--worker-image" not in user_data
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


def test_managed_pool_storage_destruction_requires_exact_volume_absence() -> None:
    ec2 = _Ec2()
    provisioner = AwsManagedPoolProvisioner(
        AwsManagedPoolClients(ec2=ec2, autoscaling=_AutoScaling())
    )
    instance_id = "i-00000000000000001"
    volume_id = "vol-00000000000000001"

    details = provisioner.instance_details((instance_id,))[instance_id]
    assert details.storage_volume_ids == (volume_id,)
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


def _pool_request(provider_ref: str) -> ProviderUnitRequest:
    return ProviderUnitRequest(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        unit_id="22345678-1234-4123-8123-123456789abc",
        unit_name=UnitName("managed-capacity"),
        provider_ref=provider_ref,
        provider_connection_id="12345678-1234-4123-8123-123456789abc",
        generation=1,
        offer=ComputeOffer(
            id="us-east-1:m7i.2xlarge",
            provider=provider_ref,
            cloud="aws",
            instance_type="m7i.2xlarge",
            region="us-east-1",
            cpu_millicores=8_000,
            memory_mb=32_768,
            storage_mb=204_800,
            cost_terms=SupplierCostTerms(compute_hourly_micros=340_000),
            available=100,
            capacity_mode=ComputeCapacityMode.Pooled,
            capability_key="aws:us-east-1:m7i.2xlarge:amd64:runsc",
            supports_scale_to_zero=True,
        ),
        desired_machines=0,
        max_machines=3,
        bootstrap=ProviderUnitBootstrap(
            control_plane_url="https://compute.example.com",
            enrollment_request_id="22345678-1234-4123-8123-123456789abc",
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64.tar.gz"
            ),
        ),
        provider_state=ComputeUnitProviderState(),
    )


def test_real_aws_offers_include_storage_and_ipv4_before_purchase() -> None:
    provider = AwsPooledCapacityProvider(
        spot_quotes=AwsSpotQuoteCache(),
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        connection=_connection_target(),
        networks={"us-east-1": _NETWORK},
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        regional_prices={
            "us-east-1": AwsRegionalPrices(
                gp3_gib_monthly_micros=80_000,
                public_ipv4_hourly_micros=5_000,
                instance_hourly_micros={"m7i.2xlarge": 340_000},
            )
        },
        client_provider=_ClientProvider(
            AwsManagedPoolClients(ec2=_Ec2(), autoscaling=_AutoScaling())
        ),
    )
    selected = choose_offer(list(provider.list_offers(root_volume_gib=200)), OfferRequest(nodes=1))
    assert selected.cost_terms.root_disk_hourly_micros == 22_223
    assert selected.cost_terms.complete_hourly_cost_micros == 367_223
    larger = choose_offer(list(provider.list_offers(root_volume_gib=400)), OfferRequest(nodes=1))
    assert larger.cost_terms.root_disk_hourly_micros == 44_445
    unpriced = replace(
        provider, regional_prices={"us-west-2": provider.regional_prices["us-east-1"]}
    )
    with pytest.raises(ValueError, match="no compute offers"):
        choose_offer(list(unpriced.list_offers(root_volume_gib=200)), OfferRequest(nodes=1))


def test_pooled_provider_preserves_capacity_across_namespace_adoption() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provider = AwsPooledCapacityProvider(
        spot_quotes=AwsSpotQuoteCache(),
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        connection=_connection_target(),
        networks={"us-east-1": _NETWORK},
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        client_provider=_ClientProvider(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling)),
    )
    request = _pool_request(provider.provider_ref)

    created = provider.set_unit_capacity(request, desired_machines=1, max_machines=3)
    original_namespace = request.workspace_id
    request = request.model_copy(
        update={"workspace_id": "platform-namespace", "provider_state": created.provider_state}
    )
    updated = provider.set_unit_capacity(
        request,
        desired_machines=2,
        max_machines=3,
    )
    assert created.desired_machines == 1
    assert updated.desired_machines == 2
    assert updated.provider_state.attributes["namespace_id"] == original_namespace
    assert updated.resource_id == created.resource_id
    assert autoscaling.create_count == 1
    assert autoscaling.update_count == 1
    assert sorted(ec2.launch_versions) == [1]
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
            "LaunchTemplate": autoscaling.launch_template,
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1b",
            "LaunchTemplate": autoscaling.launch_template,
        },
    ]
    observed_request = request.model_copy(
        update={
            "desired_machines": 2,
            "provider_state": updated.provider_state,
        }
    )
    ready = provider.describe_unit(observed_request)
    assert ready.phase is ProviderCapacityPhase.Ready
    assert {instance.status for instance in ready.instances} == {ProviderMachineStatus.Active}
    assert ready.current_template_version != ""
    assert {instance.booted_template_version for instance in ready.instances} == {
        ready.current_template_version
    }

    autoscaling.instances = [
        {
            "InstanceId": first_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1a",
            "LaunchTemplate": autoscaling.launch_template,
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "InService",
            "HealthStatus": "Unhealthy",
            "AvailabilityZone": "us-east-1b",
            "LaunchTemplate": autoscaling.launch_template,
        },
    ]
    degraded = provider.describe_unit(observed_request)
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
            "LaunchTemplate": autoscaling.launch_template,
        },
        {
            "InstanceId": second_instance,
            "LifecycleState": "Pending",
            "HealthStatus": "Healthy",
            "AvailabilityZone": "us-east-1b",
            "LaunchTemplate": autoscaling.launch_template,
        },
    ]
    launching = provider.describe_unit(observed_request)
    launching_statuses = {
        instance.provider_instance_id: instance.status for instance in launching.instances
    }
    assert launching.phase is not ProviderCapacityPhase.Ready
    assert launching_statuses[second_instance] is ProviderMachineStatus.Pending

    disabled = observed_request.model_copy(update={"purchases_enabled": False})
    provider.ensure_unit(disabled)
    assert autoscaling.suspended_processes == {"Launch"}
    provider.set_unit_capacity(observed_request, desired_machines=2, max_machines=3)
    assert autoscaling.suspended_processes == set()

    agent_release = provider.ensure_unit(
        observed_request.model_copy(
            update={
                "bootstrap": observed_request.bootstrap.model_copy(
                    update={
                        "agent_sha256": "c" * 64,
                        "agent_binary_url": "https://artifacts.lazycloud.test/agent/v2",
                    }
                )
            }
        )
    )
    assert agent_release.current_template_version == ready.current_template_version
    assert {instance.booted_template_version for instance in agent_release.instances} == {
        ready.current_template_version
    }
    provider.delete_unit(observed_request)
    deleted = provider.delete_unit(observed_request)
    assert deleted.phase is ProviderCapacityPhase.Deleted
    released = provider.release_machine(observed_request, first_instance)
    assert released.phase is ProviderCapacityPhase.Deleted
    assert released.instances == []


def test_pooled_provider_refuses_a_connection_with_no_network() -> None:
    provider = AwsPooledCapacityProvider(
        spot_quotes=AwsSpotQuoteCache(),
        provider_ref="aws:12345678-1234-4123-8123-123456789abc",
        networks={},
        connection=AwsAccountConnectionTarget(
            account_id="123456789012",
            region="us-east-1",
            role_arn="arn:aws:iam::123456789012:role/lazycloud-control",
            external_id=SecretStr("x" * 48),
            node_role_arn="arn:aws:iam::123456789012:role/lazycloud-node",
            node_instance_profile_arn=("arn:aws:iam::123456789012:instance-profile/lazycloud-node"),
        ),
        binaries_by_region={
            "us-east-1": AwsManagedPoolBinaries(
                agent_version="0.1.0",
                agent_sha256="a" * 64,
                cpu_ami_id="ami-0123456789abcdef0",
            )
        },
        client_provider=_ClientProvider(
            AwsManagedPoolClients(ec2=_Ec2(), autoscaling=_AutoScaling())
        ),
    )

    with pytest.raises(ValueError, match="network is not configured for 'us-east-1'"):
        provider.set_unit_capacity(
            _pool_request(provider.provider_ref),
            desired_machines=1,
            max_machines=3,
        )


@pytest.mark.parametrize(
    "host_change",
    [
        {"ami_id": "ami-00000000000000002"},
        {"root_volume_gib": 250},
        {"node_instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/replacement"},
        {"security_group_id": "sg-00000000000000002"},
    ],
)
def test_managed_pool_replaces_hosts_for_host_configuration_changes(
    host_change: dict[str, str | int],
) -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    initial_spec = _spec()
    created = provisioner.ensure(initial_spec)
    autoscaling.instances = [
        {
            "InstanceId": "i-00000000000000001",
            "LifecycleState": "InService",
            "HealthStatus": "Healthy",
            "ProtectedFromScaleIn": True,
            "LaunchTemplate": dict(autoscaling.launch_template),
        }
    ]
    upgraded_spec = initial_spec.model_copy(
        update={
            "bootstrap": initial_spec.bootstrap.model_copy(
                update={
                    "agent_version": "0.2.0",
                    "agent_sha256": "c" * 64,
                    "agent_binary_url": "https://artifacts.lazycloud.test/agent/v2",
                }
            )
        }
    )

    upgraded = provisioner.ensure(upgraded_spec, created.resource_ids)
    repeated = provisioner.ensure(upgraded_spec, upgraded.resource_ids)

    assert created.resource_ids.launch_template_latest_version == 1
    assert upgraded.resource_ids.launch_template_latest_version == 2
    assert repeated.resource_ids.launch_template_latest_version == 2
    assert sorted(ec2.launch_versions) == [1, 2]
    assert ec2.default_launch_version == 2
    assert autoscaling.launch_template == {
        "LaunchTemplateId": "lt-00000000000000001",
        "Version": "2",
    }
    assert autoscaling.update_count == 1
    assert upgraded.instances[0].booted_template_version == "1"
    assert upgraded.current_host_revision == created.current_host_revision
    assert upgraded.instances[0].booted_host_revision == upgraded.current_host_revision

    replaced = provisioner.ensure(upgraded_spec.model_copy(update=host_change))
    assert replaced.current_host_revision != upgraded.current_host_revision
    assert replaced.instances[0].booted_host_revision == created.current_host_revision


def test_managed_pool_refuses_replacement_without_host_configuration_evidence() -> None:
    ec2 = _Ec2()
    autoscaling = _AutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    spec = _spec()
    provisioner.ensure(spec)
    autoscaling.instances = [{"InstanceId": "i-00000000000000001"}]
    with pytest.raises(AwsProviderControlError, match="instance launch template is unavailable"):
        provisioner.describe(spec)
    autoscaling.instances = [
        {
            "InstanceId": "i-00000000000000001",
            "LaunchTemplate": autoscaling.launch_template,
        }
    ]
    description, data = ec2.launch_versions[1]
    ec2.launch_versions[1] = (description, {**data, "UserData": "invalid bootstrap"})
    with pytest.raises(AwsProviderControlError, match="bootstrap evidence is invalid"):
        provisioner.describe(spec)


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


def test_managed_pool_finishes_cleanup_when_group_disappears_during_delete() -> None:
    class _DisappearingAutoScaling(_AutoScaling):
        def delete_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
            self.exists = False
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": (
                            "AutoScalingGroup name not found - "
                            f"AutoScalingGroup '{self.name}' not found"
                        ),
                    }
                },
                "DeleteAutoScalingGroup",
            )

    ec2 = _Ec2()
    autoscaling = _DisappearingAutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    spec = _spec(desired_nodes=0)
    provisioner.ensure(spec)

    provisioner.delete(spec)
    assert provisioner.delete(spec).phase is AwsManagedPoolPhase.Deleted
    assert not ec2.launch_template


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
    with pytest.raises(AwsProviderControlError, match="not owned"):
        provisioner.ensure(_spec().model_copy(update={"purchases_enabled": False}))
    assert autoscaling.suspended_processes == set()


def test_reenable_keeps_launches_suspended_when_capacity_update_fails() -> None:
    class _FailingAutoScaling(_AutoScaling):
        def update_auto_scaling_group(self, **kwargs: object) -> Mapping[str, object]:
            raise AwsProviderControlError(
                AwsProviderControlErrorCode.UpstreamUnavailable,
                operation="update Auto Scaling Group",
                detail="capacity API unavailable",
            )

    ec2 = _Ec2()
    autoscaling = _FailingAutoScaling()
    provisioner = AwsManagedPoolProvisioner(AwsManagedPoolClients(ec2=ec2, autoscaling=autoscaling))
    spec = _spec(desired_nodes=2)
    provisioner.ensure(spec)
    provisioner.ensure(spec.model_copy(update={"purchases_enabled": False}))

    with pytest.raises(AwsManagedPoolProvisioningError, match="capacity API unavailable"):
        provisioner.ensure(spec.model_copy(update={"desired_nodes": 1, "root_volume_gib": 100}))

    assert autoscaling.suspended_processes == {"Launch"}
    assert autoscaling.desired == 2
    assert autoscaling.launch_template["Version"] == "1"


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


class _StoppingEc2(_RetainedEc2):
    """Records each stop request, refusing hibernation with `refusal` when given."""

    def __init__(self, *, refusal: str) -> None:
        super().__init__()
        self.refusal = refusal
        self.stops: list[str] = []

    def stop_instances(
        self, *, InstanceIds: list[str], Hibernate: bool = False, Force: bool = False
    ) -> Mapping[str, object]:
        if Hibernate and self.refusal:
            raise ClientError({"Error": {"Code": self.refusal, "Message": "no"}}, "StopInstances")
        self.stops.append("force" if Force else "hibernate" if Hibernate else "stop")
        for instance_id in InstanceIds:
            self.instances[instance_id]["State"] = {"Name": "stopping"}
        return {}


@pytest.mark.parametrize(
    ("hibernate", "refusal", "first_pass", "after_deadline"),
    [
        (True, "", ["hibernate"], ["hibernate", "force"]),
        (True, "UnsupportedHibernationConfiguration", ["stop"], ["stop", "force"]),
        (True, "UnsupportedOperation", [], ["stop"]),
        (False, "", ["stop"], ["stop", "force"]),
    ],
)
def test_a_reserve_hibernates_only_when_asked_and_always_reaches_a_stop(
    retained_pool: tuple[AwsRetainedPool, _RetainedEc2],
    hibernate: bool,
    refusal: str,
    first_pass: list[str],
    after_deadline: list[str],
) -> None:
    """A reserve hibernates only when compute asked, and it never stays stopping.

    An instance launched without hibernation stops plainly at once. Any other
    refusal, such as a guest not ready to hibernate, is retried until the
    operation deadline and then stops plainly. A stop of either kind still
    pending past that deadline, counted from its own request, is forced.
    """
    pool, _ = retained_pool
    ec2 = _StoppingEc2(refusal=refusal)
    now = utc_now()
    slot = RetainedSlot(
        token=uuid4().hex,
        created_at=now - timedelta(hours=1),
        launch_template_id="lt-retained",
        launch_template_version=1,
        host_revision="host",
        subnet_id=_SUBNET_IDS[0],
        serving=False,
        phase=SlotPhase.Stopping,
        instance_id="i-00000000000000001",
        hibernate=hibernate,
    )
    ec2.instances = {
        "i-00000000000000001": {
            "InstanceId": "i-00000000000000001",
            "ClientToken": slot.token,
            "State": {"Name": "running"},
            "LaunchTime": (now - timedelta(hours=1)).isoformat(),
            "HibernationOptions": {"Configured": True},
            "Tags": [
                {"Key": AWS_MANAGED_POOL_TAG, "Value": AWS_MANAGED_POOL_TAG_VALUE},
                {"Key": "cloud-pool:key", "Value": pool.spec.resource_key},
                {"Key": "cloud-pool:workspace", "Value": pool.spec.workspace_id},
            ],
        }
    }
    request = pool.request.model_copy(update={"purchases_enabled": False})
    clients = AwsManagedPoolClients(ec2=ec2, autoscaling=_AutoScaling())
    _save_slot(pool, request, slot)
    first = AwsRetainedPool(request, pool.spec, clients, pool.checkpoints).ensure()
    assert ec2.stops == first_pass
    # Compute rechecks only an instance whose stop the provider accepted.
    assert [instance.stop_requested for instance in first.instances] == [bool(first_pass)]
    [stopping] = RetainedPoolState.model_validate(pool.checkpoints.load(request).attributes).slots
    overdue = now - timedelta(minutes=11)
    _save_slot(
        pool,
        request,
        stopping.model_copy(
            update={
                "stop_requested_at": stopping.stop_requested_at and overdue,
                "hibernate_refused_since": stopping.hibernate_refused_since and overdue,
            }
        ),
    )
    AwsRetainedPool(request, pool.spec, clients, pool.checkpoints).ensure()

    assert ec2.stops == after_deadline

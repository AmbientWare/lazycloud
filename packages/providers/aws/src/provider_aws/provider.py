from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, TypeGuard, TypeVar, overload, runtime_checkable
from uuid import uuid4

from boto3.session import Session
from botocore.exceptions import ClientError
from compute.offers import ComputeOffer
from compute.providers import (
    DirectMachineLaunchRequest,
    ProviderMachineReference,
    ProviderMachineStatus,
    ProviderReconcileResult,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from shared.app_identity import NAME
from shared.image_building.credentials import (
    EcrRegistryRef,
    parse_ecr_registry,
    registry_hosts_equal,
)
from storage.backends import ObjectBackendKind, ObjectLocation, RangeRequest

from .ec2 import (
    DEFAULT_EC2_INSTANCE_OFFERS,
    AwsComputeRequest,
    AwsEc2Client,
    AwsEc2MachineDiscoveryPlan,
    AwsEc2MachineProvisionPlan,
    AwsEc2MachineReference,
    AwsEc2MachineTerminationPlan,
    AwsEc2TagKey,
    AwsInstanceOffer,
    AwsMachineUserData,
    describe_instance_pages,
    encode_machine_user_data,
    extract_first_instance_id,
    extract_first_instance_reference,
    instance_states_from_describe,
    instance_volume_ids_from_describe,
    machine_references_from_describe,
    select_instance_offer,
)


class AwsCredentialSource(StrEnum):
    Default = "default"
    Profile = "profile"
    Static = "static"


class AwsService(StrEnum):
    Ec2 = "ec2"
    Ecr = "ecr"
    S3 = "s3"


class AwsProviderHealthStatus(StrEnum):
    Ready = "ready"
    Degraded = "degraded"


class AwsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AwsProviderSettings(AwsModel):
    region: str = "us-east-1"
    profile: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    cluster_name: str = NAME
    ec2_ami: str = ""
    ec2_subnet_id: str | None = None
    gateway_url: str = ""
    ec2_root_volume_gib: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        has_access_key = bool(self.access_key_id)
        has_secret_key = bool(self.secret_access_key)
        if has_access_key != has_secret_key:
            msg = "AWS static credentials require both access_key_id and secret_access_key"
            raise ValueError(msg)
        if self.profile and has_access_key:
            msg = "AWS profile and static credentials cannot be used together"
            raise ValueError(msg)
        return self


class AwsSessionOptions(AwsModel):
    region_name: str
    credential_source: AwsCredentialSource
    profile_name: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    def boto3_kwargs(self) -> dict[str, str]:
        kwargs = {"region_name": self.region_name}
        if self.profile_name is not None:
            kwargs["profile_name"] = self.profile_name
        if self.aws_access_key_id is not None:
            kwargs["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key is not None:
            kwargs["aws_secret_access_key"] = self.aws_secret_access_key
        if self.aws_session_token is not None:
            kwargs["aws_session_token"] = self.aws_session_token
        return kwargs


class AwsClientOptions(AwsModel):
    service: AwsService
    session: AwsSessionOptions
    endpoint_url: str | None = None

    def boto3_kwargs(self) -> dict[str, str]:
        if self.endpoint_url is None:
            return {}
        return {"endpoint_url": self.endpoint_url}


class AwsK3sClusterPlan(AwsModel):
    name: str
    region: str
    server_instance_type: str = "t3.large"
    worker_instance_type: str = "t3.large"
    worker_count: int = 1
    database_engine: str = "postgres"
    object_buckets: list[str] = Field(default_factory=list)


class AwsS3Location(AwsModel):
    bucket: str
    key: str
    region: str
    endpoint_url: str | None = None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


class EcrAuthorizationPlan(AwsModel):
    registry: EcrRegistryRef
    region: str


class EcrAuthorization(AwsModel):
    registry: str
    username: str
    password: str
    expires_at: datetime | None = None


class _EcrAuthorizationData(AwsModel):
    authorization_token: str = Field(alias="authorizationToken")
    proxy_endpoint: str = Field(alias="proxyEndpoint")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")


class _EcrAuthorizationResponse(AwsModel):
    authorization_data: list[_EcrAuthorizationData] = Field(alias="authorizationData")


@runtime_checkable
class EcrAuthorizationClient(Protocol):
    def get_authorization_token(self, *, registryIds: list[str]) -> object: ...


class AwsProviderHealth(AwsModel):
    status: AwsProviderHealthStatus
    detail: str = ""


class AwsS3Body(Protocol):
    def read(self) -> bytes: ...


class AwsS3GetObjectResponse(Protocol):
    def __getitem__(self, key: Literal["Body"], /) -> AwsS3Body: ...


type AwsS3RequestValue = JsonValue | bytes


@runtime_checkable
class AwsS3Client(Protocol):
    def get_object(self, **kwargs: AwsS3RequestValue) -> AwsS3GetObjectResponse: ...

    def put_object(self, **kwargs: AwsS3RequestValue) -> None: ...


@dataclass
class AwsS3ObjectBackend:
    s3_client: AwsS3Client
    kind: ObjectBackendKind = ObjectBackendKind.S3Compatible

    def read_bytes(
        self,
        location: ObjectLocation,
        range_request: RangeRequest | None = None,
    ) -> bytes:
        params: dict[str, AwsS3RequestValue] = {
            "Bucket": location.bucket,
            "Key": location.key,
        }
        if range_request is not None:
            params["Range"] = range_request.header_value()
        response = self.s3_client.get_object(**params)
        return response["Body"].read()

    def write_bytes(self, location: ObjectLocation, data: bytes) -> None:
        self.s3_client.put_object(Bucket=location.bucket, Key=location.key, Body=data)


_SessionT = TypeVar("_SessionT", covariant=True)
_ClientT = TypeVar("_ClientT", covariant=True)


class AwsBoto3Module(Protocol[_SessionT]):
    def Session(self, **kwargs: str) -> _SessionT: ...


class AwsClientSession(Protocol[_ClientT]):
    def client(self, service: str, **kwargs: str) -> _ClientT: ...


class _RuntimeClientFactory(Protocol):
    def client(self, service: str, **kwargs: str) -> object: ...


def _is_runtime_client_factory(value: object) -> TypeGuard[_RuntimeClientFactory]:
    return callable(getattr(value, "client", None))


@dataclass
class AwsProvider:
    settings: AwsProviderSettings = field(default_factory=AwsProviderSettings)

    def session_options(self) -> AwsSessionOptions:
        if self.settings.access_key_id and self.settings.secret_access_key:
            return AwsSessionOptions(
                region_name=self.settings.region,
                credential_source=AwsCredentialSource.Static,
                aws_access_key_id=self.settings.access_key_id,
                aws_secret_access_key=self.settings.secret_access_key,
                aws_session_token=self.settings.session_token,
            )
        if self.settings.profile:
            return AwsSessionOptions(
                region_name=self.settings.region,
                credential_source=AwsCredentialSource.Profile,
                profile_name=self.settings.profile,
            )
        return AwsSessionOptions(
            region_name=self.settings.region,
            credential_source=AwsCredentialSource.Default,
        )

    def client_options(self, service: AwsService) -> AwsClientOptions:
        return AwsClientOptions(
            service=service,
            session=self.session_options(),
            endpoint_url=self.settings.endpoint_url,
        )

    @overload
    def session(self) -> Session: ...

    @overload
    def session(self, *, boto3_module: AwsBoto3Module[_SessionT]) -> _SessionT: ...

    def session(
        self,
        *,
        boto3_module: AwsBoto3Module[_SessionT] | None = None,
    ) -> Session | _SessionT:
        options = self.session_options().boto3_kwargs()
        if boto3_module is not None:
            return boto3_module.Session(**options)
        session_options = self.session_options()
        return Session(
            region_name=session_options.region_name,
            profile_name=session_options.profile_name,
            aws_access_key_id=session_options.aws_access_key_id,
            aws_secret_access_key=session_options.aws_secret_access_key,
            aws_session_token=session_options.aws_session_token,
        )

    @overload
    def client(
        self,
        service: AwsService,
        *,
        session: AwsClientSession[_ClientT],
    ) -> _ClientT: ...

    @overload
    def client(
        self,
        service: Literal[AwsService.Ec2],
        *,
        session: None = None,
    ) -> AwsEc2Client: ...

    @overload
    def client(
        self,
        service: Literal[AwsService.Ecr],
        *,
        session: None = None,
    ) -> EcrAuthorizationClient: ...

    @overload
    def client(
        self,
        service: Literal[AwsService.S3],
        *,
        session: None = None,
    ) -> AwsS3Client: ...

    def client(
        self,
        service: AwsService,
        *,
        session: AwsClientSession[_ClientT] | None = None,
    ) -> _ClientT | AwsEc2Client | EcrAuthorizationClient | AwsS3Client:
        options = self.client_options(service)
        if session is not None:
            return session.client(options.service.value, **options.boto3_kwargs())
        source: object = self.session()
        if not _is_runtime_client_factory(source):
            raise RuntimeError("boto3 session lacks the client factory operation")
        candidate = source.client(options.service.value, **options.boto3_kwargs())
        if service is AwsService.Ec2 and isinstance(candidate, AwsEc2Client):
            return candidate
        if service is AwsService.Ecr and isinstance(candidate, EcrAuthorizationClient):
            return candidate
        if service is AwsService.S3 and isinstance(candidate, AwsS3Client):
            return candidate
        raise RuntimeError(f"boto3 {service.value} client lacks required operations")

    def k3s_cluster_plan(
        self,
        name: str,
        *,
        worker_count: int = 1,
        object_buckets: list[str] | None = None,
    ) -> AwsK3sClusterPlan:
        return AwsK3sClusterPlan(
            name=name,
            region=self.settings.region,
            worker_count=worker_count,
            object_buckets=object_buckets or [f"{name}-objects", f"{name}-images"],
        )

    def available_ec2_instances(self) -> list[AwsInstanceOffer]:
        return list(DEFAULT_EC2_INSTANCE_OFFERS)

    def list_offers(self) -> list[ComputeOffer]:
        return [
            ComputeOffer(
                id=offer.instance_type,
                provider="aws",
                cloud="aws",
                instance_type=offer.instance_type,
                region=self.settings.region,
                cpu_millicores=offer.spec.cpu_millicores,
                memory_mb=offer.spec.memory_mb,
                gpu=offer.spec.gpu or None,
                gpu_count=offer.spec.gpu_count,
                node_count=1,
                available=1,
            )
            for offer in self.available_ec2_instances()
        ]

    def select_ec2_instance(
        self,
        request: AwsComputeRequest,
        *,
        offers: Sequence[AwsInstanceOffer] | None = None,
    ) -> AwsInstanceOffer:
        return select_instance_offer(request, offers=offers)

    def user_data_base64(self, config: AwsMachineUserData) -> str:
        return encode_machine_user_data(config)

    def provision_machine_plan(
        self,
        *,
        pool_name: str,
        registration_token: str,
        compute: AwsComputeRequest,
        machine_id: str | None = None,
        operation_id: str | None = None,
        image_id: str | None = None,
        subnet_id: str | None = None,
    ) -> AwsEc2MachineProvisionPlan:
        selected = self.select_ec2_instance(compute)
        resolved_machine_id = machine_id or uuid4().hex[:8]
        resolved_image_id = image_id or self.settings.ec2_ami
        if not resolved_image_id:
            msg = "AWS EC2 provisioning requires an AMI image id"
            raise ValueError(msg)
        if not self.settings.gateway_url:
            msg = "AWS EC2 provisioning requires a gateway URL"
            raise ValueError(msg)
        tags = {
            AwsEc2TagKey.Name.value: (
                f"{self.settings.cluster_name}-{pool_name}-{resolved_machine_id}"
            ),
            AwsEc2TagKey.ClusterName.value: self.settings.cluster_name,
            AwsEc2TagKey.PoolName.value: pool_name,
            AwsEc2TagKey.MachineId.value: resolved_machine_id,
            **self.settings.tags,
        }
        user_data = self.user_data_base64(
            AwsMachineUserData(
                registration_token=registration_token,
                machine_id=resolved_machine_id,
                gateway_url=self.settings.gateway_url,
                install_nvidia_runtime=bool(selected.spec.gpu),
            )
        )
        return AwsEc2MachineProvisionPlan(
            machine_id=resolved_machine_id,
            operation_id=operation_id or resolved_machine_id,
            pool_name=pool_name,
            instance_type=selected.instance_type,
            image_id=resolved_image_id,
            subnet_id=subnet_id if subnet_id is not None else self.settings.ec2_subnet_id,
            user_data_base64=user_data,
            tags=tags,
            root_volume_gib=self.settings.ec2_root_volume_gib,
        )

    def _launch_plan(self, request: DirectMachineLaunchRequest) -> AwsEc2MachineProvisionPlan:
        return self.provision_machine_plan(
            pool_name=request.pool_name,
            registration_token=request.registration_token,
            compute=AwsComputeRequest(
                cpu_millicores=request.offer.cpu_millicores,
                memory_mb=request.offer.memory_mb,
                gpu=request.offer.gpu or "",
                gpu_count=request.offer.gpu_count,
            ),
            machine_id=request.machine_id,
            operation_id=request.operation_id,
        )

    def provision_machine(
        self,
        plan: AwsEc2MachineProvisionPlan,
        *,
        ec2_client: AwsEc2Client | None = None,
    ) -> str:
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        result = client.run_instances(**plan.run_instances_kwargs())
        return extract_first_instance_id(result)

    def launch_machine(
        self,
        request: DirectMachineLaunchRequest,
        *,
        ec2_client: AwsEc2Client | None = None,
    ) -> ProviderMachineReference:
        plan = self._launch_plan(request)
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        result = client.run_instances(**plan.run_instances_kwargs())
        instance_id, storage_volume_ids = extract_first_instance_reference(result)
        if not storage_volume_ids:
            storage_volume_ids = instance_volume_ids_from_describe(
                client.describe_instances(InstanceIds=[instance_id]),
                instance_id,
            )
        if not storage_volume_ids:
            raise RuntimeError("EC2 did not expose authoritative instance storage identities")
        return ProviderMachineReference(
            provider_instance_id=instance_id,
            machine_id=plan.machine_id,
            status=ProviderMachineStatus.Pending,
            storage_volume_ids=storage_volume_ids,
        )

    def machine_discovery_plan(self, pool_name: str) -> AwsEc2MachineDiscoveryPlan:
        return AwsEc2MachineDiscoveryPlan(
            cluster_name=self.settings.cluster_name,
            pool_name=pool_name,
        )

    def list_machines(
        self,
        pool_name: str,
        *,
        ec2_client: AwsEc2Client | None = None,
    ) -> list[AwsEc2MachineReference]:
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        kwargs = self.machine_discovery_plan(pool_name).describe_instances_kwargs()
        return machine_references_from_describe(describe_instance_pages(client, kwargs))

    def terminate_machine(
        self,
        instance_id: str,
        *,
        ec2_client: AwsEc2Client | None = None,
    ) -> None:
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        plan = AwsEc2MachineTerminationPlan(instance_id=instance_id)
        client.terminate_instances(**plan.terminate_instances_kwargs())

    def machine_storage_destroyed(
        self,
        instance_id: str,
        storage_volume_ids: tuple[str, ...],
        *,
        ec2_client: AwsEc2Client | None = None,
    ) -> bool:
        if not storage_volume_ids:
            return False
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        try:
            response = client.describe_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            error = exc.response.get("Error")
            code = str(error.get("Code") or "") if isinstance(error, dict) else ""
            if code != "InvalidInstanceID.NotFound":
                raise
        else:
            states = instance_states_from_describe(response)
            if states and any(state != "terminated" for state in states):
                return False
        for volume_id in storage_volume_ids:
            try:
                volume_response = client.describe_volumes(VolumeIds=[volume_id])
            except ClientError as exc:
                error = exc.response.get("Error")
                code = str(error.get("Code") or "") if isinstance(error, dict) else ""
                if code == "InvalidVolume.NotFound":
                    continue
                raise
            volumes = volume_response.get("Volumes")
            if isinstance(volumes, list) and volumes:
                return False
            return False
        return True

    def health(self, *, ec2_client: AwsEc2Client | None = None) -> AwsProviderHealth:
        client = ec2_client if ec2_client is not None else self.client(AwsService.Ec2)
        try:
            client.describe_regions(AllRegions=False)
        except Exception as exc:
            return AwsProviderHealth(
                status=AwsProviderHealthStatus.Degraded,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return AwsProviderHealth(status=AwsProviderHealthStatus.Ready)

    def reconcile_machines(
        self,
        pool_name: str,
        expected_machine_ids: set[str],
        *,
        terminate_stale: bool = False,
        ec2_client: AwsEc2Client | None = None,
    ) -> ProviderReconcileResult:
        machines = self.list_machines(pool_name, ec2_client=ec2_client)
        remote_ids = {machine.machine_id for machine in machines}
        stale = sorted(remote_ids - expected_machine_ids)
        terminated: list[str] = []
        if terminate_stale:
            by_machine_id = {machine.machine_id: machine for machine in machines}
            for machine_id in stale:
                self.terminate_machine(
                    by_machine_id[machine_id].instance_id,
                    ec2_client=ec2_client,
                )
                if self.machine_storage_destroyed(
                    by_machine_id[machine_id].instance_id,
                    by_machine_id[machine_id].storage_volume_ids,
                    ec2_client=ec2_client,
                ):
                    terminated.append(machine_id)
        return ProviderReconcileResult(
            observed_machines=[
                ProviderMachineReference(
                    provider_instance_id=machine.instance_id,
                    machine_id=machine.machine_id,
                    storage_volume_ids=machine.storage_volume_ids,
                )
                for machine in machines
            ],
            missing_machine_ids=sorted(expected_machine_ids - remote_ids),
            stale_machine_ids=stale,
            terminated_machine_ids=terminated,
        )

    def s3_location(self, bucket: str, key: str) -> AwsS3Location:
        return AwsS3Location(
            bucket=bucket,
            key=key,
            region=self.settings.region,
            endpoint_url=self.settings.endpoint_url,
        )

    def s3_backend(self) -> AwsS3ObjectBackend:
        return AwsS3ObjectBackend(self.client(AwsService.S3))

    def ecr_authorization_plan(self, registry_host: str) -> EcrAuthorizationPlan:
        registry = parse_ecr_registry(registry_host)
        if registry is None:
            msg = f"not an ECR registry host: {registry_host}"
            raise ValueError(msg)
        return EcrAuthorizationPlan(
            registry=registry,
            region=registry.region,
        )

    def ecr_authorization(
        self,
        registry_host: str,
        *,
        ecr_client: EcrAuthorizationClient | None = None,
    ) -> EcrAuthorization:
        plan = self.ecr_authorization_plan(registry_host)
        client = ecr_client or self.client(AwsService.Ecr)
        response = _EcrAuthorizationResponse.model_validate(
            client.get_authorization_token(registryIds=[plan.registry.account_id])
        )
        expected_registry = plan.registry.host.lower()
        entry = next(
            (
                item
                for item in response.authorization_data
                if registry_hosts_equal(item.proxy_endpoint, expected_registry)
            ),
            None,
        )
        if entry is None:
            raise RuntimeError(f"ECR did not return authorization for {plan.registry.host}")
        try:
            decoded = base64.b64decode(entry.authorization_token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("ECR returned an invalid authorization token") from exc
        username, separator, password = decoded.partition(":")
        if not separator or not username or not password:
            raise RuntimeError("ECR authorization token is missing username/password material")
        return EcrAuthorization(
            registry=plan.registry.host,
            username=username,
            password=password,
            expires_at=entry.expires_at,
        )


__all__ = [
    "AwsClientOptions",
    "AwsCredentialSource",
    "AwsK3sClusterPlan",
    "AwsProvider",
    "AwsProviderHealth",
    "AwsProviderHealthStatus",
    "AwsProviderSettings",
    "AwsS3Location",
    "AwsS3ObjectBackend",
    "AwsService",
    "AwsSessionOptions",
    "EcrAuthorization",
    "EcrAuthorizationClient",
    "EcrAuthorizationPlan",
]

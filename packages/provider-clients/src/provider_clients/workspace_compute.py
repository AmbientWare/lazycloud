from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from agent.binary import AgentBinarySettings
from compute.policy import ComputeCatalogInstance, ComputeCatalogRegion
from compute.providers import ComputeProviderResolver, ResolvedComputeProvider
from networking.settings import (
    BackendRouteSettings,
    ProviderNetworkClass,
    TailnetControlSettings,
    TailnetRuntimeSettings,
    validate_provider_network_configuration,
)
from provider_aws import (
    AWS_INSTANCE_CATALOG,
    AwsAccountConnectionTarget,
    AwsConnectedAccountPooledProvider,
    AwsInstanceCategory,
    AwsManagedPoolBinaries,
    Boto3AwsManagedPoolClientProvider,
)
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
)
from shared.compute_policy import ComputeCapacityMode

from provider_clients.settings import AwsCapacitySettings

AwsConnectionLoader = Callable[[str], Iterable[AwsAccountConnection]]


def configured_aws_compute_catalog(
    capacity_settings: AwsCapacitySettings,
    agent_binary_settings: AgentBinarySettings,
) -> tuple[ComputeCatalogRegion, ...]:
    # A deployment without AWS capacity has no AWS catalog to advertise. The settings
    # validator has already rejected a partially configured one, so this is absence
    # rather than a switch.
    if not capacity_settings.configured:
        return ()
    capacity_settings.binaries_by_region(agent_binary_settings)

    priced_instance_types = capacity_settings.instance_hourly_micros.keys()
    regions = sorted(capacity_settings.cpu_ami_ids.keys() | capacity_settings.gpu_ami_ids.keys())
    catalog: list[ComputeCatalogRegion] = []
    for region in regions:
        cpu_available = region in capacity_settings.cpu_ami_ids
        gpu_available = region in capacity_settings.gpu_ami_ids
        instances_by_type: dict[str, ComputeCatalogInstance] = {}
        for instance in AWS_INSTANCE_CATALOG:
            launchable = (
                cpu_available if instance.kind is AwsInstanceCategory.Cpu else gpu_available
            )
            if not launchable or instance.instance_type not in priced_instance_types:
                continue
            instances_by_type.setdefault(
                instance.instance_type,
                ComputeCatalogInstance(
                    instance_type=instance.instance_type,
                    kind=instance.kind.value,
                    cpu_millicores=instance.cpu_millicores,
                    memory_mb=instance.memory_mb,
                    gpu=instance.gpu.value if instance.gpu is not None else None,
                    gpu_count=instance.gpu_count,
                ),
            )
        instances = tuple(instances_by_type.values())
        if instances:
            catalog.append(ComputeCatalogRegion(region=region, instances=instances))
    return tuple(catalog)


@dataclass(frozen=True, slots=True)
class WorkspaceComputeProviderResolver(ComputeProviderResolver):
    connections: AwsConnectionLoader
    binaries_by_region: Mapping[str, AwsManagedPoolBinaries]
    instance_hourly_micros: Mapping[str, int]
    allowed_instance_types: frozenset[str]
    client_provider: Boto3AwsManagedPoolClientProvider

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        return [
            self._resolved(connection)
            for connection in self.connections(workspace_id)
            if _connection_ready(connection)
        ]

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        for connection in self.connections(workspace_id):
            if _provider_ref(connection.id) != provider_ref:
                continue
            if not _connection_resolvable(connection):
                raise RuntimeError("AWS account connection is not available")
            return self._resolved(connection)
        raise KeyError(f"workspace compute provider not found: {provider_ref}")

    def _resolved(self, connection: AwsAccountConnection) -> ResolvedComputeProvider:
        authorization = connection.active_authorization
        if authorization is None:
            raise RuntimeError("ready AWS account connection has no active authorization")
        region = sorted(self.binaries_by_region)[0]
        managed = authorization.managed_authorization
        target = AwsAccountConnectionTarget(
            account_id=connection.account_id,
            region=region,
            role_arn=authorization.role_arn,
            external_id=SecretStr(connection.external_id),
            node_role_arn=connection.node_role_arn or "",
            node_instance_profile_arn=connection.node_instance_profile_arn or "",
            vpc_id=managed.vpc_id if managed is not None else None,
            subnet_ids=managed.subnet_ids if managed is not None else (),
            security_group_id=managed.security_group_id if managed is not None else None,
        )
        provider_ref = _provider_ref(connection.id)
        return ResolvedComputeProvider(
            ref=provider_ref,
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=connection.id,
            pooled=AwsConnectedAccountPooledProvider(
                provider_ref=provider_ref,
                connection=target,
                binaries_by_region=self.binaries_by_region,
                instance_hourly_micros=self.instance_hourly_micros,
                allowed_instance_types=self.allowed_instance_types,
                client_provider=self.client_provider,
            ),
        )


def workspace_compute_provider_resolver(
    capacity_settings: AwsCapacitySettings,
    agent_binary_settings: AgentBinarySettings,
    *,
    connections: AwsConnectionLoader,
    gateway_origin: str,
    internal_origin: str,
    presigned_origin: str = "",
    tailnet_runtime: TailnetRuntimeSettings,
    tailnet_control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> WorkspaceComputeProviderResolver:
    validate_provider_network_configuration(
        ProviderNetworkClass.Remote,
        gateway_origin=gateway_origin,
        internal_origin=internal_origin,
        presigned_origin=presigned_origin,
        runtime=tailnet_runtime,
        control=tailnet_control,
        backend_route=backend_route,
    )
    artifacts = capacity_settings.binaries_by_region(agent_binary_settings)
    return WorkspaceComputeProviderResolver(
        connections=connections,
        binaries_by_region=artifacts,
        instance_hourly_micros=capacity_settings.instance_hourly_micros,
        allowed_instance_types=frozenset(),
        client_provider=Boto3AwsManagedPoolClientProvider.from_default_chain(),
    )


def _provider_ref(connection_id: str) -> str:
    return f"aws:{connection_id}"


def _connection_ready(connection: AwsAccountConnection) -> bool:
    return (
        connection.hosts_workloads
        and connection.node_role_arn is not None
        and connection.node_instance_profile_arn is not None
    )


def _connection_resolvable(connection: AwsAccountConnection) -> bool:
    return (
        connection.can_manage_existing_capacity
        and connection.active_authorization is not None
        and connection.active_authorization.phase
        in {
            AwsAccountAuthorizationPhase.Ready,
            AwsAccountAuthorizationPhase.Retiring,
        }
        and connection.node_role_arn is not None
        and connection.node_instance_profile_arn is not None
    )


__all__ = [
    "WorkspaceComputeProviderResolver",
    "configured_aws_compute_catalog",
    "workspace_compute_provider_resolver",
]

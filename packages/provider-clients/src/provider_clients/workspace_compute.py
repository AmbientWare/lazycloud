from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from agent.binary import AgentBinarySettings
from compute.aws_configuration import AWS_COMPUTE_CONFIGURATION
from compute.catalog import ComputeCatalogInstance, ComputeCatalogRegion
from compute.provider_launches import ProviderNodeLaunchCredentials
from compute.providers import (
    ComputeProviderResolver,
    ResolvedComputeProvider,
    ResolvedProviderPolicy,
)
from coordination.redis_client import RedisClient
from coordination.request_cooldown import RedisRequestCooldown
from networking.settings import (
    BackendRouteSettings,
    validate_remote_provider_network_configuration,
)
from provider_aws import (
    AWS_INSTANCE_CATALOG,
    AwsAccountConnectionTarget,
    AwsConnectedAccountPooledProvider,
    AwsInstanceCategory,
    AwsManagedPoolBinaries,
    AwsRegionalPrices,
    Boto3AwsManagedPoolClientProvider,
)
from provider_aws.supplier_prices import AWS_REGIONAL_PRICES
from provider_hetzner.client import HetznerClient
from provider_hetzner.pooled_provider import HetznerPooledProvider
from provider_hetzner.supplier_prices import PRIMARY_IPV4_HOURLY_MICROS, USD_PER_CURRENCY_UNIT
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
)
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, ComputeCapacityMode, MachinePool

from provider_clients.provider_definitions import PROVIDER_DEFINITIONS
from provider_clients.settings import AwsCapacitySettings, PlatformCapacitySettings

AwsConnectionLoader = Callable[[str], Iterable[AwsAccountConnection]]
PlatformAwsConnectionLoader = Callable[[], Iterable[AwsAccountConnection]]
PlatformProviderLoader = Callable[[], tuple[ResolvedComputeProvider, ...]]


def configured_platform_compute_providers(
    settings: PlatformCapacitySettings,
    *,
    launch_credentials: ProviderNodeLaunchCredentials,
    capacity_workspace: Callable[[str], str],
    redis: RedisClient,
) -> PlatformProviderLoader:
    definition = PROVIDER_DEFINITIONS["hetzner"]
    if not settings.configured:
        return tuple
    token = settings.hetzner_tokens.get(definition.platform_ref)
    if token is None or not token.get_secret_value().strip():
        raise ValueError(f"{definition.platform_ref} requires a provider token")
    if (
        definition.policy.purchases_enabled
        and not set(definition.policy.allowed_regions) <= settings.hetzner_images.keys()
    ):
        raise ValueError(f"{definition.platform_ref} requires images for its approved regions")
    adapter = HetznerPooledProvider(
        provider_ref=definition.platform_ref,
        client=HetznerClient(token, cooldown=RedisRequestCooldown(redis, definition.platform_ref)),
        images_by_location=settings.hetzner_images,
        usd_per_currency_unit=USD_PER_CURRENCY_UNIT,
        primary_ipv4_hourly_micros=PRIMARY_IPV4_HOURLY_MICROS,
        launch_credentials=launch_credentials,
    )

    def providers() -> tuple[ResolvedComputeProvider, ...]:
        # Administrator bootstrap creates the capacity workspace after composing
        # services. Resolve its identity when capacity is used, never at startup.
        return (
            ResolvedComputeProvider(
                ref=definition.platform_ref,
                capacity_mode=ComputeCapacityMode.Pooled,
                policy=ResolvedProviderPolicy(
                    **definition.policy.model_dump(),
                    workspace_id=capacity_workspace(definition.workspace),
                    pool=MachinePool(LAZYCLOUD_MACHINE_POOL),
                    platform_fleet=True,
                ),
                pooled=adapter,
            ),
        )

    return providers


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

    regions = sorted(capacity_settings.cpu_ami_ids.keys() | capacity_settings.gpu_ami_ids.keys())
    catalog: list[ComputeCatalogRegion] = []
    for region in regions:
        prices = AWS_REGIONAL_PRICES.get(region)
        priced_instance_types = prices.instance_hourly_micros if prices is not None else {}
        if region not in AWS_COMPUTE_CONFIGURATION.allowed_regions:
            continue
        cpu_available = region in capacity_settings.cpu_ami_ids
        gpu_available = region in capacity_settings.gpu_ami_ids
        instances_by_type: dict[str, ComputeCatalogInstance] = {}
        for instance in AWS_INSTANCE_CATALOG:
            launchable = (
                cpu_available if instance.kind is AwsInstanceCategory.Cpu else gpu_available
            )
            if not launchable or (
                instance.instance_type not in priced_instance_types
                and True not in instance.purchase_markets
            ):
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
    platform_connections: PlatformAwsConnectionLoader
    binaries_by_region: Mapping[str, AwsManagedPoolBinaries]
    client_provider: Boto3AwsManagedPoolClientProvider
    capacity_workspace: Callable[[AwsAccountConnection], str]
    platform_providers: PlatformProviderLoader = tuple
    regional_prices: Mapping[str, AwsRegionalPrices] = field(
        default_factory=lambda: AWS_REGIONAL_PRICES
    )

    def list_platform_providers(self) -> Iterable[ResolvedComputeProvider]:
        providers = (
            *self.platform_providers(),
            *(
                self._resolved(connection)
                for connection in self.platform_connections()
                if self.binaries_by_region and _connection_ready(connection)
            ),
        )
        refs = [provider.ref for provider in providers]
        if len(refs) != len(set(refs)):
            raise ValueError("platform compute provider refs must be unique")
        if any(
            provider.policy is None or not provider.policy.platform_fleet for provider in providers
        ):
            raise ValueError("platform compute providers require platform-owned policies")
        return providers

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        providers = {provider.ref: provider for provider in self.list_platform_providers()}
        for connection in self.connections(workspace_id):
            if (
                self.binaries_by_region
                and _connection_ready(connection)
                and _provider_ref(connection.id) not in providers
            ):
                provider = self._resolved(connection)
                providers[provider.ref] = provider
        return tuple(providers.values())

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        for provider in self.platform_providers():
            if provider.ref == provider_ref:
                return provider
        for connection in (*self.platform_connections(), *self.connections(workspace_id)):
            if _provider_ref(connection.id) != provider_ref:
                continue
            if not _connection_resolvable(connection):
                raise RuntimeError("AWS account connection is not available")
            return self._resolved(connection)
        raise KeyError(f"workspace compute provider not found: {provider_ref}")

    def _resolved(self, connection: AwsAccountConnection) -> ResolvedComputeProvider:
        if not self.binaries_by_region:
            raise RuntimeError("AWS capacity release artifacts are unavailable")
        authorization = connection.active_authorization
        if authorization is None:
            raise RuntimeError("ready AWS account connection has no active authorization")
        region = sorted(self.binaries_by_region)[0]
        target = AwsAccountConnectionTarget(
            account_id=connection.account_id,
            region=region,
            role_arn=authorization.role_arn,
            external_id=SecretStr(connection.external_id),
            node_role_arn=connection.node_role_arn or "",
            node_instance_profile_arn=connection.node_instance_profile_arn or "",
            network=connection.network,
        )
        provider_ref = _provider_ref(connection.id)
        return ResolvedComputeProvider(
            ref=provider_ref,
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=connection.id,
            policy=ResolvedProviderPolicy(
                **PROVIDER_DEFINITIONS["aws"]
                .policy.model_copy(
                    update={
                        "allowed_offers": PROVIDER_DEFINITIONS["aws"].policy.allowed_offers
                        if _connection_ready(connection)
                        else ()
                    }
                )
                .model_dump(),
                workspace_id=self.capacity_workspace(connection),
                pool=connection.pool,
                platform_fleet=connection.platform_fleet,
            ),
            pooled=AwsConnectedAccountPooledProvider(
                provider_ref=provider_ref,
                connection=target,
                binaries_by_region=self.binaries_by_region,
                regional_prices=self.regional_prices,
                client_provider=self.client_provider,
            ),
        )


def workspace_compute_provider_resolver(
    capacity_settings: AwsCapacitySettings,
    agent_binary_settings: AgentBinarySettings,
    *,
    connections: AwsConnectionLoader,
    platform_connections: PlatformAwsConnectionLoader,
    capacity_workspace: Callable[[AwsAccountConnection], str],
    gateway_origin: str,
    presigned_origin: str = "",
    backend_route: BackendRouteSettings,
    platform_providers: PlatformProviderLoader = tuple,
) -> WorkspaceComputeProviderResolver:
    validate_remote_provider_network_configuration(
        gateway_origin=gateway_origin,
        presigned_origin=presigned_origin,
        backend_route=backend_route,
    )
    artifacts = (
        capacity_settings.binaries_by_region(agent_binary_settings)
        if capacity_settings.configured
        else {}
    )
    return WorkspaceComputeProviderResolver(
        connections=connections,
        platform_connections=platform_connections,
        capacity_workspace=capacity_workspace,
        platform_providers=platform_providers,
        binaries_by_region=artifacts,
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
    "configured_platform_compute_providers",
    "workspace_compute_provider_resolver",
]

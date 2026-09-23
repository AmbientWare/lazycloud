from collections.abc import Iterator
from contextlib import contextmanager

from compute.aws_connections import AwsAccountConnectionDirectory
from compute.provider_state import ProviderUnitStateService
from compute.service import ComputeService
from coordination.redis_client import RedisClient, RedisSettings
from database.context import ServiceContext
from gateway.settings import GatewaySettings
from identity.platform import PlatformNamespaceService
from provider_clients.release import resolve_deployment_release
from provider_clients.settings import PlatformCapacitySettings
from provider_clients.workspace_compute import (
    configured_platform_compute_providers,
    workspace_compute_provider_resolver,
)
from scheduler.capacity_reservations import RedisCapacityReservationRepository

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@contextmanager
def platform_compute() -> Iterator[ComputeService]:
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin).direct()
    )
    redis = RedisClient.from_settings(RedisSettings())
    try:
        context = ServiceContext.create(database)
        platform_namespace_id = PlatformNamespaceService(database).namespace_id
        directory = AwsAccountConnectionDirectory(context)
        release = resolve_deployment_release()

        def namespace_id() -> str:
            return platform_namespace_id

        resolver = workspace_compute_provider_resolver(
            release.aws_capacity,
            release.agent_binaries,
            connections=directory.list_for_workspace,
            capacity_workspace=directory.capacity_workspace,
            gateway_origin=GatewaySettings().public_http_url,
            platform_providers=configured_platform_compute_providers(
                PlatformCapacitySettings(),
                provider_state=ProviderUnitStateService(database),
                capacity_workspace=namespace_id,
                binaries_by_region=(
                    release.aws_capacity.binaries_by_region(release.agent_binaries)
                    if release.aws_capacity.configured
                    else {}
                ),
            ),
        )
        yield ComputeService(
            context,
            provider_resolver=resolver,
            capacity_owner_mutations=RedisCapacityReservationRepository(redis),
        )
    finally:
        redis.close()
        database.dispose()

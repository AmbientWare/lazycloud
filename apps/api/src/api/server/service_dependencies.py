from __future__ import annotations

from typing import Annotated

from compute.aws_connections import AwsAccountConnectionDirectory, AwsAccountConnectionService
from compute.fleet_status import FleetStatusService
from compute.policy import WorkspaceComputePolicyService
from control.service import ControlPlaneService
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from execution.pods.service import PodControlService
from execution.shells.service import ShellControlService
from execution.task_rerun import TaskRerunService
from execution.volumes.control import VolumeControlService
from fastapi import Depends
from gateway.containers import GatewayContainerService
from gateway.deployments import GatewayDeploymentService
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from gateway.service import GatewayControlService
from gateway.tunnel_certificates import TunnelCertificateService
from images.control import ImageControlService
from networking.dialer import BackendRouteDialer
from operations.tasks import TaskManagementService
from provider_clients.settings import (
    AWS_CONNECTION_CONTROL_PRINCIPAL_ENV,
    RELEASE_MANIFEST_URL_ENV,
)
from scheduler.autoscaler_operations import AutoscalerOperationsService
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.workers import SchedulerWorkerAdminService
from shared.errors import UpstreamUnavailableError

from api.server.dependencies import (
    api_services,
    route_services,
    runtime_services,
)
from api.server.services import (
    ApiRoutes,
    ApiServiceCore,
    ApiServices,
    EndpointApiService,
    ExecutionRoutes,
    FunctionApiService,
    ManagementRoutes,
    RuntimeRoutes,
    RuntimeServiceCore,
)
from api.server.worker_repository_service import (
    WorkerRepositoryService,
)


def endpoint_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> EndpointApiService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("endpoint_service is not owned by this API")
    return services.endpoint_service


def fleet_status_service(
    services: Annotated[RuntimeServiceCore, Depends(runtime_services)],
) -> FleetStatusService:
    if services.compute.reserve_state is None:
        raise UpstreamUnavailableError("Fleet scheduler state is not configured")
    return FleetStatusService(services.context.database, services.compute.reserve_state)


def control_plane_service(
    services: Annotated[ApiServiceCore, Depends(api_services)],
) -> ControlPlaneService:
    return services.control_plane_service


def aws_account_connection_service(
    services: Annotated[RuntimeServiceCore, Depends(runtime_services)],
) -> AwsAccountConnectionService:
    if services.aws_connections is None:
        # The values rather than the capability. A deployment reaching this is
        # missing something nameable, and naming it is the difference between an
        # operator reading one line and an operator searching for a setting.
        raise UpstreamUnavailableError(
            "this deployment has no connected AWS: it needs "
            f"{AWS_CONNECTION_CONTROL_PRINCIPAL_ENV} and a release naming a connection "
            f"template through {RELEASE_MANIFEST_URL_ENV}"
        )
    return services.aws_connections


def aws_account_connection_directory(
    services: Annotated[RuntimeServiceCore, Depends(runtime_services)],
) -> AwsAccountConnectionDirectory:
    return services.aws_account_connection_directory


def workspace_compute_policy_service(
    services: Annotated[ApiServiceCore, Depends(api_services)],
) -> WorkspaceComputePolicyService:
    return services.workspace_compute_policy_service


def provider_node_enrollment_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> ProviderNodeEnrollmentService:
    if not isinstance(services, (RuntimeRoutes, ApiServices)):
        raise RuntimeError("provider_node_enrollment_service is not owned by this API")
    if services.provider_node_enrollment_service is None:
        raise UpstreamUnavailableError("Provider-backed compute is not enabled")
    return services.provider_node_enrollment_service


def machine_lifecycle_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> MachineLifecycleService:
    if not isinstance(services, (ManagementRoutes, RuntimeRoutes, ApiServices)):
        raise RuntimeError("machine_lifecycle_service is not owned by this API")
    return services.machine_lifecycle_service


def function_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> FunctionApiService:
    return services.function_service


def gateway_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> GatewayControlService:
    if not isinstance(services, (RuntimeRoutes, ManagementRoutes, ApiServices)):
        raise RuntimeError("gateway control is not owned by this API")
    return services.gateway_service


def gateway_deployment_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> GatewayDeploymentService:
    if not isinstance(services, (ManagementRoutes, ApiServices)):
        raise RuntimeError("gateway_deployment_service is not owned by this API")
    return services.gateway_deployment_service


def tunnel_certificate_service(
    services: Annotated[ApiServiceCore, Depends(api_services)],
) -> TunnelCertificateService:
    return services.tunnel_certificate_service


def image_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> ImageControlService:
    if not isinstance(services, (ManagementRoutes, ApiServices)):
        raise RuntimeError("image_service is not owned by this API")
    return services.image_service


def artifact_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> ArtifactStorageService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("artifact_service is not owned by this API")
    return services.artifact_service


def pod_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> PodControlService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("pod_service is not owned by this API")
    return services.pod_service


def map_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> RedisMapService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("map_service is not owned by this API")
    return services.map_service


def simple_queue_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> RedisSimpleQueueService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("simple_queue_service is not owned by this API")
    return services.simple_queue_service


def shell_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> ShellControlService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("shell_service is not owned by this API")
    return services.shell_service


def backend_route_resolver(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> SchedulerBackendRouteResolver:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("backend_route_resolver is not owned by this API")
    return services.backend_route_resolver


def backend_route_dialer(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> BackendRouteDialer:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("backend_route_dialer is not owned by this API")
    return services.backend_route_dialer


def task_rerun_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> TaskRerunService:
    if not isinstance(services, (ExecutionRoutes, ManagementRoutes, ApiServices)):
        raise RuntimeError("task_rerun_service is not owned by this API")
    return services.task_rerun_service


def volume_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> VolumeControlService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("volume_service is not owned by this API")
    return services.volume_service


def worker_repository_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> WorkerRepositoryService:
    if not isinstance(services, (RuntimeRoutes, ApiServices)):
        raise RuntimeError("worker_repository_service is not owned by this API")
    return services.worker_repository_service


def autoscaler_operations_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> AutoscalerOperationsService:
    if not isinstance(services, (ManagementRoutes, ApiServices)):
        raise RuntimeError("autoscaler_operations_service is not owned by this API")
    return services.autoscaler_operations_service


def scheduler_worker_admin_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> SchedulerWorkerAdminService:
    if not isinstance(services, (ManagementRoutes, ApiServices)):
        raise RuntimeError("worker administration is not owned by this API")
    return services.scheduler_worker_admin_service


def gateway_container_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> GatewayContainerService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("gateway containers are not owned by this API")
    return services.gateway_container_service


def task_management_service(
    services: Annotated[ApiRoutes, Depends(route_services)],
) -> TaskManagementService:
    if not isinstance(services, (ExecutionRoutes, ApiServices)):
        raise RuntimeError("task management is not owned by this API")
    return services.task_management_service
